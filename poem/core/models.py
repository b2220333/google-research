# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Defines model architectures."""

import functools

import numpy as np
import tensorflow.compat.v1 as tf

from poem.core import common
from poem.core import data_utils


def _linear(input_features, output_size, weight_max_norm, weight_initializer,
            bias_initializer, name):
  """Builds a linear layer.

  Args:
    input_features: A tensor for input features. Shape = [..., feature_dim].
    output_size: An integer for the number of output nodes.
    weight_max_norm: A float for the maximum weight norm to clip at. Use
      non-positive to ignore.
    weight_initializer: A function handle for kernel weight initializer.
    bias_initializer: A function handle for bias initializer.
    name: A string for the name scope.

  Returns:
    A tensor for the output logits.
  """
  with tf.variable_scope(name, reuse=tf.AUTO_REUSE):
    weights = tf.get_variable(
        name='weight',
        shape=[input_features.shape.as_list()[-1], output_size],
        initializer=weight_initializer)
    if weight_max_norm > 0.0:
      weights = tf.clip_by_norm(weights, clip_norm=weight_max_norm)

    bias = tf.get_variable(
        name='bias', shape=[output_size], initializer=bias_initializer)

  return tf.linalg.matmul(input_features, weights) + bias


def simple_base(input_features, is_training, name='SimpleModel', **kwargs):
  """Implements `simple baseline` model base architecture.

  Note that the code differs from the original architecture by disabling dropout
  and maximum weight norms by default.

  Reference:
    Martinez et al. A Simple Yet Effective Baseline for 3D Human Pose
    Estimation. https://arxiv.org/pdf/1705.03098.pdf.

  Args:
    input_features: A tensor for input features. Shape = [..., feature_dim].
    is_training: A boolean for whether it is in training mode.
    name: A string for the name scope.
    **kwargs: A dictionary for additional arguments. Supported arguments include
      `num_hidden_nodes`, `weight_initializer`, `bias_initializer`,
      `weight_max_norm`, `use_batch_norm`, `dropout_rate`, `num_fcs_per_block`,
      and `num_fc_blocks`.

  Returns:
    A tensor for output activations. Shape = [..., output_dim].
  """

  def fully_connected(input_features, is_training, name):
    """Builds a fully connected layer."""
    net = _linear(
        input_features,
        output_size=kwargs.get('num_hidden_nodes', 1024),
        weight_max_norm=kwargs.get('weight_max_norm', 0.0),
        weight_initializer=kwargs.get('weight_initializer',
                                      tf.initializers.he_normal()),
        bias_initializer=kwargs.get('bias_initializer',
                                    tf.initializers.he_normal()),
        name=name + '/Linear')

    if kwargs.get('use_batch_norm', True):
      net = tf.layers.batch_normalization(
          net,
          training=is_training,
          name=name + '/BatchNorm',
          reuse=tf.AUTO_REUSE)

    net = tf.nn.relu(net, name=name + '/Relu')

    dropout_rate = kwargs.get('dropout_rate', 0.0)
    if is_training and dropout_rate > 0.0:
      net = tf.nn.dropout(net, rate=dropout_rate, name=name + '/Dropout')
    return net

  def fully_connected_block(input_features, is_training, name):
    """Builds a fully connected layer block."""
    net = input_features
    for i in range(kwargs.get('num_fcs_per_block', 2)):
      net = fully_connected(
          net, is_training=is_training, name=name + '/FC_%d' % i)
    net += input_features
    return net

  net = fully_connected(
      input_features, is_training=is_training, name=name + '/InputFC')
  for i in range(kwargs.get('num_fc_blocks', 2)):
    net = fully_connected_block(
        net, is_training, name=name + '/FullyConnectedBlock_%d' % i)
  return net


def simple_model(input_features,
                 output_sizes,
                 is_training,
                 name='SimpleModel',
                 num_bottleneck_nodes=0,
                 **kwargs):
  """Implements `simple base` model with outputs.

  Note that the code differs from the original architecture by disabling dropout
  and maximum weight norms by default.

  Args:
    input_features: A tensor for input features. Shape = [..., feature_dim].
    output_sizes: A dictionary for output sizes in the format {output_name:
      output_size}, where `output_size` can be an integer or a list.
    is_training: A boolean for whether it is in training mode.
    name: A string for the name scope.
    num_bottleneck_nodes: An integer for size of the bottleneck layer to be
      added before the output layer(s). No bottleneck layer will be added if
      non-positive.
    **kwargs: A dictionary of additional arguments passed to `simple_base`.

  Returns:
    outputs: A dictionary for output tensors in the format {output_name:
      output_tensors}. Output tensor shape = [..., output_size].
    activations: A dictionary of addition activation tensors for pre-output
      model activations. Keys include 'base_activations' and optionally
      'bottleneck_activations'.
  """
  net = simple_base(
      input_features, is_training=is_training, name=name, **kwargs)
  activations = {'base_activations': net}

  if num_bottleneck_nodes > 0:
    net = _linear(
        net,
        output_size=num_bottleneck_nodes,
        weight_max_norm=kwargs.get('weight_max_norm', 0.0),
        weight_initializer=kwargs.get('weight_initializer',
                                      tf.initializers.he_normal()),
        bias_initializer=kwargs.get('bias_initializer',
                                    tf.initializers.he_normal()),
        name=name + '/BottleneckLogits')
    activations['bottleneck_activations'] = net

  outputs = {}
  for output_name, output_size in output_sizes.items():
    if isinstance(output_size, int):
      output_size = [output_size]
    outputs[output_name] = _linear(
        net,
        output_size=np.prod(output_size),
        weight_max_norm=kwargs.get('weight_max_norm', 0.0),
        weight_initializer=kwargs.get('weight_initializer',
                                      tf.initializers.he_normal()),
        bias_initializer=kwargs.get('bias_initializer',
                                    tf.initializers.he_normal()),
        name=name + '/OutputLogits/' + output_name)
    if len(output_size) > 1:
      outputs[output_name] = data_utils.recursively_expand_dims(
          outputs[output_name], axes=[-1] * (len(output_size) - 1))
      outputs[output_name] = data_utils.reshape_by_last_dims(
          outputs[output_name], last_dim_shape=output_size)
  return outputs, activations


_add_prefix = lambda key, c: 'C%d/' % c + key


def simple_point_embedder(input_features, num_embedding_components,
                          embedding_size, is_training, **kwargs):
  """Implements a point embedder based on `simple model`.

  Output tensor shapes:
    KEY_EMBEDDING_MEANS: Shape = [..., num_embedding_components, embedding_dim].

  Args:
    input_features: A tensor for input features. Shape = [..., feature_dim].
    num_embedding_components: An integer for the number of embedding components.
    embedding_size: An integer for embedding dimensionality.
    is_training: A boolean for whether it is in training mode.
    **kwargs: A dictionary of additional arguments passed to `simple_model`.

  Returns:
    outputs: A dictionary for output tensors See comment above for details.
    activations: A dictionary of addition activation tensors for pre-output
      model activations. Keys include 'base_activations' and optionally
      'bottleneck_activations'.
  """
  output_sizes = {
      _add_prefix(common.KEY_EMBEDDING_MEANS, c): embedding_size
      for c in range(num_embedding_components)
  }

  component_outputs, activations = simple_model(
      input_features, output_sizes, is_training=is_training, **kwargs)

  outputs = {
      common.KEY_EMBEDDING_MEANS:
          tf.stack([
              component_outputs[_add_prefix(common.KEY_EMBEDDING_MEANS, c)]
              for c in range(num_embedding_components)
          ],
                   axis=-2)
  }
  return outputs, activations


def simple_gaussian_embedder(input_features,
                             num_embedding_components,
                             embedding_size,
                             num_embedding_samples,
                             is_training,
                             seed=None,
                             **kwargs):
  """Implements a Gaussian (mixture) embedder based on `simple model`.

  Output tensor shapes:
    KEY_EMBEDDING_MEANS: Shape = [..., num_embedding_components,
      embedding_dim].
    KEY_EMBEDDING_STDDEVS: Shape = [..., num_embedding_components,
      embedding_dim].
    KEY_EMBEDDING_SAMPLES: Shape = [..., num_embedding_components, num_samples,
      embedding_dim].

  Args:
    input_features: A tensor for input features. Shape = [..., feature_dim].
    num_embedding_components: An integer for the number of Gaussian mixture
      components.
    embedding_size: An integer for embedding dimensionality.
    num_embedding_samples: An integer for number of samples drawn Gaussian
      distributions. If non-positive, skips the sampling step.
    is_training: A boolean for whether it is in training mode.
    seed: An integer for random seed.
    **kwargs: A dictionary of additional arguments passed to `simple_base`.

  Returns:
    outputs: A dictionary for output tensors See comment above for details.
    activations: A dictionary of addition activation tensors for pre-output
      model activations. Keys include 'base_activations' and optionally
      'bottleneck_activations'.
  """
  output_sizes = {}
  for c in range(num_embedding_components):
    output_sizes.update({
        _add_prefix(common.KEY_EMBEDDING_MEANS, c): embedding_size,
        _add_prefix(common.KEY_EMBEDDING_STDDEVS, c): embedding_size,
    })
  component_outputs, activations = simple_model(
      input_features, output_sizes, is_training=is_training, **kwargs)

  for c in range(num_embedding_components):
    component_outputs[_add_prefix(common.KEY_EMBEDDING_STDDEVS, c)] = (
        tf.nn.elu(component_outputs[_add_prefix(common.KEY_EMBEDDING_STDDEVS,
                                                c)]) + 1.0)

    if num_embedding_samples > 0:
      component_outputs[_add_prefix(common.KEY_EMBEDDING_SAMPLES, c)] = (
          data_utils.sample_gaussians(
              means=component_outputs[_add_prefix(common.KEY_EMBEDDING_MEANS,
                                                  c)],
              stddevs=component_outputs[_add_prefix(
                  common.KEY_EMBEDDING_STDDEVS, c)],
              num_samples=num_embedding_samples,
              seed=seed))

  outputs = {
      common.KEY_EMBEDDING_MEANS:
          tf.stack([
              component_outputs[_add_prefix(common.KEY_EMBEDDING_MEANS, c)]
              for c in range(num_embedding_components)
          ],
                   axis=-2),
      common.KEY_EMBEDDING_STDDEVS:
          tf.stack([
              component_outputs[_add_prefix(common.KEY_EMBEDDING_STDDEVS, c)]
              for c in range(num_embedding_components)
          ],
                   axis=-2),
  }
  if num_embedding_samples > 0:
    outputs[common.KEY_EMBEDDING_SAMPLES] = tf.stack([
        component_outputs[_add_prefix(common.KEY_EMBEDDING_SAMPLES, c)]
        for c in range(num_embedding_components)
    ],
                                                     axis=-3)

  return outputs, activations


def create_embedder(embedding_type, num_embedding_components, embedding_size):
  """Creates an embedding model builder function handle.

  Args:
    embedding_type: An enum string for embedding type. See supported embedding
      types in the `common` module.
    num_embedding_components: An integer for the number of embedding components.
    embedding_size: An integer for embedding dimensionality.

  Returns:
    A function handle for embedding model builder.

  Raises:
    ValueError: If embedding type is not supported.
  """
  if embedding_type == common.EMBEDDING_TYPE_POINT:
    return functools.partial(
        simple_point_embedder,
        num_embedding_components=num_embedding_components,
        embedding_size=embedding_size)
  if embedding_type == common.EMBEDDING_TYPE_GAUSSIAN:
    return functools.partial(
        simple_gaussian_embedder,
        num_embedding_components=num_embedding_components,
        embedding_size=embedding_size)

  raise ValueError('Unsupported embedding type: `%s`.' % str(embedding_type))


def embed(input_features, embedding_type, num_embedding_components,
          embedding_size, **kwargs):
  """An embedder wrapper with input/output transformation handling.

  Args:
    input_features: A tensor for input features. Shape = [batch_size,
      feature_dim] or [batch_size, num_instances, feature_dim].
    embedding_type: An enum string for embedding type. See supported embedding
      types in the `common` module.
    num_embedding_components: An integer for the number of embedding components.
    embedding_size: An integer for embedding dimensionality.
    **kwargs: A dictionary for additional arguments for embedder.

  Returns:
    outputs: A dictionary for output tensors See comment above for details.
    activations: A dictionary of addition activation tensors for pre-output
      model activations. Keys include 'base_activations' and optionally
      'bottleneck_activations'.
  """
  input_features_rank = len(input_features.shape.as_list())
  if input_features_rank not in [2, 3]:
    raise ValueError('Only supports input feature tensors of rank 2 or 3: %d.' %
                     input_features_rank)

  embedder_fn = create_embedder(embedding_type, num_embedding_components,
                                embedding_size)

  if input_features_rank == 2:
    return embedder_fn(input_features, **kwargs)

  sub_output_list, sub_activation_list = [], []
  for sub_features in tf.unstack(input_features, axis=1):
    sub_outputs, sub_activations = embedder_fn(sub_features, **kwargs)
    sub_output_list.append(sub_outputs)
    sub_activation_list.append(sub_activations)

  outputs = {}
  for key in sub_output_list[0].keys():
    outputs[key] = tf.stack([sub_output[key] for sub_output in sub_output_list],
                            axis=1)

  activations = {}
  for key in sub_activation_list[0].keys():
    activations[key] = tf.stack(
        [sub_activations[key] for sub_activations in sub_activation_list],
        axis=1)

  return outputs, activations
