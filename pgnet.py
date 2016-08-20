#Copyright (C) 2016 Paolo Galeone <nessuno@nerdz.eu>
#
#This Source Code Form is subject to the terms of the Mozilla Public
#License, v. 2.0. If a copy of the MPL was not distributed with this
#file, you can obtain one at http://mozilla.org/MPL/2.0/.
#Exhibit B is not attached; this software is compatible with the
#licenses expressed under Section 1.12 of the MPL v2.
"""
The model is fully convolutional, thus it accepts batch of images of any size and produces
a spatial map of vector.
The degenerate case is when the input size have the same size of the train images. In that case
the output is a (batchsize x)1x1x<num_classes> tensor

Conventions:
    var_: placeholder
"""

import math
import os
import sys
import tensorflow as tf
import freeze_graph
import utils

# network constants
INPUT_SIDE = 184
INPUT_DEPTH = 3

KERNEL_SIDE = 3
LAST_KERNEL_SIDE = 25
LAST_CONV_STRIDE = 1

DOWNSAMPLING_FACTOR = math.ceil(INPUT_SIDE / LAST_KERNEL_SIDE)
FC_NEURONS = 2048

# train constants
BATCH_SIZE = 256
LEARNING_RATE = 1e-4  # Initial learning rate.

# output tensor name
OUTPUT_TENSOR_NAME = "softmax_linear/out"

# name of the collection that holds non trainable
# but required variables for the current model
REQUIRED_NON_TRAINABLES = 'required_vars_collection'


def batch_norm(layer_output, is_training_):
    """Applies batch normalization to the layer output.
    Args:
        layer_output: 4-d tensor, output of a FC/convolutional layer
        is_training_: placeholder or boolean variable to set to True when training
    """
    return tf.contrib.layers.batch_norm(
        layer_output,
        decay=0.999,
        center=True,
        scale=True,
        epsilon=0.001,
        activation_fn=None,
        # update moving mean and variance in place
        updates_collections=None,
        is_training=is_training_,
        reuse=None,
        # create a collections of varialbes to save
        # (moving mean and moving variance)
        variables_collections=[REQUIRED_NON_TRAINABLES],
        outputs_collections=None,
        trainable=True,
        scope=None)


# conv_layer do not normalize its output
def conv_layer(input_x, kernel_shape, padding, strides):
    """Returns the result of:
    ReLU(conv2d(input_x, kernels, padding=padding, strides) + bias).
    Creates kernels (name=kernel), bias (name=bias) and relates summaries.

    Args:
        input_x: 4-D input tensor. shape = [batch, height, width, depth]
        kernel_shape: the shape of W, used in convolution as kernels:
                [kernel_height, kernel_width, kernel_depth, num_kernels]
        name: the op name
        padding; "VALID" | "SAME"
        stride: 4-d tensor, like: [1, 2, 2, 1]
    """

    num_kernels = kernel_shape[3]

    kernels = utils.kernels(kernel_shape, "kernels")
    bias = utils.bias([num_kernels], "bias")

    out = tf.nn.relu(
        tf.add(tf.nn.conv2d(
            input_x, kernels, strides=strides, padding=padding),
               bias),
        name="out")
    return out


def prepad(input_x, kernel_side):
    """Returns input_x, padded with the right amount of zeros such that
    a convolution of input_x padded, with kernel side, returns an output
    with the same size of input_x.
    Args:
        input_x: 4d tensor [batch_size, widht, height, depth]
        kernel_side: the side of the convolutional kernel
    """
    # pad the input with the right amount of padding
    pad_amount = int((kernel_side - 1) / 2)
    input_padded = tf.pad(input_x, [[0, 0], [pad_amount, pad_amount],
                                    [pad_amount, pad_amount], [0, 0]],
                          name="input_padded")
    return input_padded


# eq_conv_layer do normalize its output
def eq_conv_layer(input_x, kernel_side, num_kernels, strides, is_training_):
    """Pads the input with the right amount of zeros.
    Convolve the padded input. In that way every pixel of the input image
    will contribute on average.
    Output WxH = input WxH.
    Args:
        input_x: image batch
        kernel_side: kernel side
        num_kernels: the number of the kernels to learn
        strides: 4d tensor like: [1, stride, stride, 1]
        is_training_: boolean placeholder to enable/disable train changes
    Returns:
        batch_norm(conv_layer(input_padded))
    """

    with tf.variable_scope("eq_conv_layer"):
        kernel_shape = [kernel_side, kernel_side, input_x.get_shape()[3].value,
                        num_kernels]

        input_padded = prepad(input_x, kernel_side)
        print(input_padded)
        out = batch_norm(
            conv_layer(input_padded, kernel_shape, "VALID", strides),
            is_training_)

        return out


def atrous_layer(input_x, atrous_kernel_shape, rate, padding, is_training_):
    """
    Returns the result of:
    ReLU(atrous_conv2d(x, kernels, rate, padding=padding) + bias).
    Creates kernels (name=kernel), bias (name=bias) and relates summaries.

    output[b, i, j, k] = sum_{di, dj, q} filters[di, dj, q, k] *
         value[b, i + rate * di, j + rate * dj, q]
    Thus: if rate=1, atrous_conv2d = conv2d with stride [1,1,1,1]

    Supposing a 3x3 filter:
    In that case, a (3+rate)x(3+rate)x num_kernels is convolved with the input
    Thus the perception filed of every filter is 5x5xd (rate=2),
    but the number of parameters is 3x3xd
    eg: 5 = (filter_h) + (filter_h -1)*(rate -1)

    Args:
        x: 4-D input tensor. shape = [batch, height, width, depth]
        atrous_kernel_shape: the shape of W [kernel_height, kernel_width, kernel_depth, num_kernels]
        rate: the atrous_conv2d rate parameter
        padding: "VALID" | "SAME"
        is_training_: placeholder to enable/disable train changes
    """

    with tf.variable_scope("atrous_conv_layer"):
        num_kernels = atrous_kernel_shape[3]

        kernels = utils.kernels(atrous_kernel_shape, "kernels")
        bias = utils.bias([num_kernels], "bias")

        return batch_norm(tf.nn.relu(
            tf.add(tf.nn.atrous_conv2d(
                input_x,
                kernels,
                rate=rate,  # rate = 2 means 1 hole
                padding=padding),
                   bias)), is_training_)


def get(num_classes, images_, keep_prob_, is_training_, train_phase=False):
    """
    Args:
        num_classes: is the number of classes that the network will classify
        images_: is a tensor placeholder with shape [-1, widht, height, depth]
        keep_prob_: dropout probability
        is_training_: placeholder to enable/disable train changes
        train_phase: set it to True when training.

    As the net goes deeper, increase the number of filters (using power of 2
    in order to optimize GPU performance).

    @returns:
        softmax_linear/out: spatial map of output vectors (unscaled)
    """

    # in order to have only the images_ placeholder reqired when using
    # the exported model, override is_training_ is_training when the model
    # is not in the train phase. So the placeholder is not required by the model
    if train_phase is False:
        is_training_ = False

    # 184x184x3
    print(images_)

    num_kernels = 2**7  #128
    with tf.variable_scope(str(num_kernels)):
        with tf.variable_scope("conv1"):
            conv1 = eq_conv_layer(images_, KERNEL_SIDE, num_kernels,
                                  [1, 2, 2, 1], is_training_)
        #output: 92x92x128, filters: (3x3x3)x128
        print(conv1)

    num_kernels *= 2  #256
    with tf.variable_scope(str(num_kernels)):
        with tf.variable_scope("conv2"):
            conv2 = eq_conv_layer(conv1, KERNEL_SIDE, num_kernels,
                                  [1, 2, 2, 1], is_training_)
        #output: 46x46x256, filters: (3x3x128)x256
        print(conv2)

    num_kernels *= 2  #512
    with tf.variable_scope(str(num_kernels)):
        with tf.variable_scope("conv3"):
            conv3 = eq_conv_layer(conv2, KERNEL_SIDE, num_kernels,
                                  [1, 2, 2, 1], is_training_)
        #output: 23x23x512, filters: (3x3x256)x512
        print(conv3)

    atrous_kernel_side = 5
    rate = 6
    # equivalent side = 5 + (5-1)*(6-1) = 25
    # = LAST_KERNEL_SIDE. But the paremeters are not 25x25, but only 5x5
    with tf.variable_scope('last'):
        # if the dummy filter is 3, pad amount is 2P = 2 => P=1 per side
        conv3 = prepad(conv3, 3)

        print(conv3)
        # 25x25x512
        with tf.variable_scope("conv4"):
            conv4 = atrous_layer(
                conv3, [atrous_kernel_side, atrous_kernel_side, num_kernels,
                        FC_NEURONS], rate, 'VALID', is_training_)
        print(conv4)
        # output: 1x1xFC_NEURONS, filters: (25x25x512)x512, but parameters: (5x5x512)x512
        # combine 512 features (extracted from the 25x25x512 filters) into FC_NEURONS

    # 1x1xDepth convolutions, FC equivalent
    with tf.variable_scope("fc1"):
        fc1 = conv_layer(conv4, [1, 1, FC_NEURONS, FC_NEURONS], "VALID",
                         [1, 1, 1, 1])
        # remove dependece from the keep_prob_ placeholder when the model
        # is not in train phase
        if train_phase is False:
            dropout1 = fc1
        else:
            dropout1 = tf.nn.dropout(fc1, keep_prob_, name="dropout")

    print(dropout1)
    # output: 1x1xNUM_NEURONS

    with tf.variable_scope("fc2"):
        fc2 = conv_layer(dropout1, [1, 1, FC_NEURONS, FC_NEURONS], "VALID",
                         [1, 1, 1, 1])
        # output: 1x1xNUM_NEURONS
        # remove dependece from the keep_prob_ placeholder when the model
        # is not in train phase
        if train_phase is False:
            dropout2 = fc2
        else:
            dropout2 = tf.nn.dropout(fc2, keep_prob_, name="dropout")

    print(dropout2)
    # output: 1x1xNUM_NEURONS

    with tf.variable_scope("softmax_linear"):
        out = conv_layer(dropout2, [1, 1, FC_NEURONS, num_classes], "VALID",
                         [1, 1, 1, 1])
    # output: (BATCH_SIZE)x1x1xnum_classes if the input has been properly scaled
    # otherwise is a map
    print(out)

    return out


def loss(logits, labels):
    """
    Args:
        logits: Logits from get().
        labels: Labels from distorted_inputs or inputs(). 1-D tensor of shape [batch_size]

    Returns:
        Loss tensor of type float.
    """

    with tf.variable_scope("loss"):
        # remove dimension of size 1 from logits tensor
        # since logits tensor is: (BATCH_SIZE)x1x1xnum_classes
        # remove dimension in position 1 and 2
        logits = tf.squeeze(logits, [1, 2])
        labels = tf.cast(labels, tf.int64)

        # cross_entropy across the batch
        cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
            logits, labels, name="cross_entropy_per_example")

        mean_cross_entropy = tf.reduce_mean(
            cross_entropy, name="mean_cross_entropy")
        tf.scalar_summary("loss/mean_cross_entropy", mean_cross_entropy)

    return mean_cross_entropy


def train(loss_op, global_step):
    """
    Creates an Optimizer.
    Args:
        loss_op: loss from loss()
        global_step: integer variable counting the numer of traning steps processed

    Returns:
        train_op: of for training
    """
    # Variables that affect learning rate.
    with tf.variable_scope("train"):
        optimizer = tf.train.AdamOptimizer(LEARNING_RATE)
        # minimizes loss and increments global_step by 1
        minimizer = optimizer.minimize(loss_op, global_step=global_step)

    return minimizer


def variables_to_save(addlist):
    """Create a list of all trained variables and required variables of the model.
    Appends to the list, the addlist passed as argument.

    Args:
        addlist: [list, of, variables, to, save]
    Returns:
        a a list of variables"""

    return tf.trainable_variables() + tf.get_collection_ref(
        REQUIRED_NON_TRAINABLES) + addlist


def define_model(num_classes, train_phase):
    """ define the model with its inputs.
    Use this function to define the model in training and when exporting the model
    in the protobuf format.

    Args:
        num_classes: number of classes
        train_phase: set it to True when defining the model, during train

    Return:
        is_training_: enable/disable training placeholder. Useful for evaluation
        keep_prob_: model dropout placeholder
        images_: input images placeholder
        logits: the model output
    """
    is_training_ = tf.placeholder(tf.bool, shape=[], name="is_training_")
    keep_prob_ = tf.placeholder(tf.float32, shape=[], name="keep_prob_")
    images_ = tf.placeholder(
        tf.float32,
        shape=[None, INPUT_SIDE, INPUT_SIDE, INPUT_DEPTH],
        name="images_")

    # build a graph that computes the logits predictions from the images
    logits = get(num_classes,
                 images_,
                 keep_prob_,
                 is_training_,
                 train_phase=train_phase)
    return is_training_, keep_prob_, images_, logits


def export_model(num_classes, session_dir, input_checkpoint, model_filename):
    """Export model defines the model in a new empty graph.
    Creates a saver for the model.
    Restores the session if exists, otherwise prints an error and returns -1
    Once the session has beeen restored, writes in the session_dir the graphs skeleton
    and creates the model.pb file, that holds the computational graph of the model and
    its inputs.

    Args:
        num_classes: number of classes of the trained model
        session_dir: absolute path of the checkpoint folder
        input_checkpoint: the name of the latest checkpoint (the desidered checkpoint to restore).
                          will look into session_dir/input_checkpoint (eg: session_dir/model-0)
        model_filename: the name of the freezed model
    """
    # if the trained model does not exist
    if not os.path.exists(model_filename):
        # create an empty graph into the CPU because GPU can run OOM
        graph = tf.Graph()
        with graph.as_default(), tf.device('/cpu:0'):
            # inject in the default graph the model structure
            define_model(num_classes, train_phase=False)
            # create a saver, to restore the graph in the session_dir
            saver = tf.train.Saver(tf.all_variables())

            # create a new session
            with tf.Session(config=tf.ConfigProto(
                    allow_soft_placement=True)) as sess:
                try:
                    saver.restore(sess, session_dir + "/" + input_checkpoint)
                except ValueError:
                    print(
                        "[E] Unable to restore from checkpoint",
                        file=sys.stderr)
                    return -1

                # save model skeleton (the empty graph, its definition)
                tf.train.write_graph(
                    graph.as_graph_def(),
                    session_dir,
                    "skeleton.pbtxt",
                    as_text=True)

                freeze_graph.freeze_graph(
                    session_dir + "/skeleton.pbtxt", "", False,
                    session_dir + "/" + input_checkpoint, OUTPUT_TENSOR_NAME,
                    "save/restore_all", "save/Const:0", model_filename, True,
                    "")
    else:
        print("{} already exists. Skipping export_model".format(
            model_filename))
