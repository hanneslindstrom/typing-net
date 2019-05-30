"""
Builds a siamese CNN and trains it to embed typing data from same user to be similar,
and typing data from different users to be dissimilar.

Adapted from:
https://github.com/divyashan/time_series/blob/master/models/supervised/siamese_triplet_keras.py
"""

import os
import signal
import argparse
import random

import numpy as np
import h5py

import keras
from keras.models import Model
from keras.layers import Dense, Input, Lambda
from keras.layers import Conv1D, MaxPooling1D, Flatten
from keras.layers.normalization import BatchNormalization
from keras.optimizers import Adam
from keras.callbacks import Callback, ModelCheckpoint
import keras.backend as K

import utils

# Constants
PERIOD = 10

# Parameters
ALPHA = 1  # Triplet loss threshold
LEARNING_RATE = 3e-6
EPOCHS = 100
BATCH_SIZE = 64

# Global variables
stop_flag = False  # Flag to indicate that training was terminated early
training_complete = False  # Flag to indicate that training is complete


class OnlineTripletGenerator(keras.utils.Sequence):
    """
    Generates semi-hard triplets online by feeding random triplets through triplet_model,
    keeping ones that are semi-hard. This is repeated until a full batch is generated, at which
    point the full batch is returned.

    Semi-hard: 0 <= loss <= ALPHA

    Code is adapted from: https://omoindrot.github.io/triplet-loss
    """

    def __init__(self, data_path, dataset_name, tower_model, batch_size=300, alpha=ALPHA):
        "Initialization"

        self.tower_model = tower_model
        self.data_file = h5py.File(data_path, "r")
        self.X_name = "X_" + dataset_name
        self.y_name = "y_" + dataset_name

        self.n_examples = self.data_file[self.X_name].shape[0]
        self.example_length = self.data_file[self.X_name].shape[1]
        self.n_features = self.data_file[self.X_name].shape[2]
        self.n_classes = self.data_file[self.y_name].shape[1]

        self.batch_size = batch_size

        self.indices = list(range(self.n_examples))

        self.on_epoch_end()

    def __len__(self):
        "Denotes the number of batches per epoch"
        return int(np.ceil(self.n_examples / self.batch_size))

    def __getitem__(self, index):
        "Generate one batch of data"

        # Generate indexes of the batch
        batch_indices = self.indices[index * self.batch_size: (index + 1) * self.batch_size]

        self.this_batch_size = len(batch_indices)

        # Load the raw examples
        X_boolean_mask = np.zeros((self.n_examples, self.example_length, self.n_features), dtype=bool)
        X_boolean_mask[batch_indices, :, :] = True
        X_batch = self.data_file[self.X_name][X_boolean_mask].reshape((self.this_batch_size, self.example_length, self.n_features))
        y_boolean_mask = np.zeros((self.n_examples, self.n_classes), dtype=bool)
        y_boolean_mask[batch_indices, :] = True
        y_batch = self.data_file[self.y_name][y_boolean_mask].reshape((self.this_batch_size, self.n_classes))

        # Compute the embeddings of this batch
        embeddings = self.tower_model.predict(X_batch)
        labels = np.array(utils.one_hot_to_index(y_batch))

        # Generate batch hard triplets
        anchor_inds, positive_inds, negative_inds = self._batch_hard(embeddings, labels)

        X_anchors = X_batch[anchor_inds, :, :]
        X_positives = X_batch[positive_inds, :, :]
        X_negatives = X_batch[negative_inds, :, :]
        y_dummy = np.zeros((self.this_batch_size,))

        return [X_anchors, X_positives, X_negatives], y_dummy

    def on_epoch_end(self):
        "Updates indexes after each epoch"
        random.shuffle(self.indices)

    def _pairwise_distances(self, embeddings):
        """
        Computes a 2D matrix of distances between all embeddings.
        """

        # Pairwise dot product between all embeddings
        dot_products = np.matmul(embeddings, embeddings.T)

        # Squared L2 norm for each embedding
        square_norms = np.diagonal(dot_products)

        # Pairwise distances
        distances = np.expand_dims(square_norms, 0) - 2.0 * dot_products + np.expand_dims(square_norms, 0)

        # Replace any negative distances with zeros
        distances = np.maximum(distances, 0)

        return distances

    def _anchor_positive_mask(self, labels):
        """
        Returns a mask of shape (batch_size, batch_size)
        where mask[a, p] is True iff. a and p are distinct
        and have the same label.
        """

        # Check if a and p are distinct
        indices_equal = np.eye(labels.shape[0])
        indices_not_equal = np.logical_not(indices_equal)

        # Check if labels[a] == labels[p]
        labels_equal = np.equal(np.expand_dims(labels, 0), np.expand_dims(labels, 1))

        # AND to get mask
        mask = np.logical_and(indices_not_equal, labels_equal)

        return mask

    def _anchor_negative_mask(self, labels):
        """
        Returns a mask of shape (batch_size, batch_size)
        where mask[a, n] is True iff. a and n have different labels.
        """

        # Check if labels[a] == labels[n]
        labels_equal = np.equal(np.expand_dims(labels, 0), np.expand_dims(labels, 1))

        mask = np.logical_not(labels_equal)

        return mask

    def _batch_hard(self, embeddings, labels):
        """
        For each anchor, select the hardest positive and hardest negative
        within the batch. Yields batch_size triplets.
        """

        # Indices of anchors
        anchor_inds = range(self.this_batch_size)

        pairwise_dists = self._pairwise_distances(embeddings)

        # For each anchor, pick hardest positive
        mask_anchor_positive = self._anchor_positive_mask(labels)
        anchor_positive_dists = np.multiply(mask_anchor_positive, pairwise_dists)  # Set 0 where (a, p) invalid
        positive_inds = np.argmax(anchor_positive_dists, axis=1)  # Find hardest positives

        # For each anchor, pick hardest negative
        mask_anchor_negative = self._anchor_negative_mask(labels)
        max_dist = np.amax(pairwise_dists, axis=1, keepdims=True)
        anchor_negative_dists = pairwise_dists + max_dist * (1.0 - mask_anchor_negative)  # Add max dist to invalid negatives
        negative_inds = np.argmin(anchor_negative_dists, axis=1)  # Find hardest negatives

        return anchor_inds, positive_inds, negative_inds


class TerminateOnFlag(Callback):
    """
    Callback that terminates training at the end of an epoch if stop_flag is encountered.
    """

    def on_batch_end(self, batch, logs=None):
        if stop_flag:
            self.model.stop_training = True


def handler(signum, frame):
    """
    Flags stop_flag if CTRL-C is received.
    """
    global training_complete

    if not training_complete:
        print('\nCTRL+C signal received. Training will finish after current batch.')
        global stop_flag
        stop_flag = True
    else:
        exit()


def setup_callbacks(save_path):
    """
    Sets up callbacks for early stopping and model saving.
    """

    signal.signal(signal.SIGINT, handler)

    callback_list = []

    callback_list.append(TerminateOnFlag())  # Terminate training if CTRL+C

    if save_path is not None:
        model_checkpoint = ModelCheckpoint(save_path + "_class_model_{epoch:02d}_{val_loss:.2f}.hdf5", monitor="val_loss", save_best_only=True, verbose=1, period=10)  # Save model every 10 epochs
        callback_list.append(model_checkpoint)

    return callback_list


def _euclidean_distance(vects):
    """
    Computes euclidean distance between tuple of vectors.
    """
    x, y = vects
    return K.sqrt(K.maximum(K.sum(K.square(x - y), axis=1, keepdims=True), K.epsilon()))


def _cosine_distance(vects):
    x, y = vects
    x = K.l2_normalize(x, axis=-1)
    y = K.l2_normalize(y, axis=-1)
    return -K.mean(x * y, axis=-1, keepdims=True)


def _cos_dist_output_shape(shapes):
    shape1, shape2, shape3 = shapes
    return (shape1[0], 1)


def _eucl_dist_output_shape(shapes):
    """
    Wat?
    """
    shape1, shape2, shape3 = shapes
    return (shape1[0], 1)


def _triplet_distance(vects):
    """
    Computes triplet loss for single triplet.
    """

    A, P, N = vects
    return K.maximum(_euclidean_distance([A, P]) - _euclidean_distance([A, N]) + ALPHA, 0.0)


def build_tower_cnn_model(input_shape):
    """
    Builds a CNN-model for embedding single examples of data.
    """

    x0 = Input(input_shape, name='Input')

    kernel = 7
    n_channels = [16, 16]
    x = x0
    for i in range(len(n_channels)):
        x = Conv1D(n_channels[i], kernel_size=kernel, strides=2, activation='relu', padding='same')(x)
        x = BatchNormalization()(x)
        x = MaxPooling1D(5)(x)

    x = Flatten()(x)
    y = Dense(40, name='dense_encoding')(x)

    model = Model(inputs=x0, outputs=y)

    return model


def build_triplet_model(input_shape, tower_model):
    """
    Builds a model that takes a triplet as input, feeds them through
    tower_model and outputs the triplet loss of the embeddings.
    """
    input_A = Input(input_shape)
    input_B = Input(input_shape)
    input_C = Input(input_shape)

    tower_model.summary()

    x_A = tower_model(input_A)
    x_B = tower_model(input_B)
    x_C = tower_model(input_C)

    distance = Lambda(_triplet_distance, output_shape=_eucl_dist_output_shape)([x_A, x_B, x_C])

    model = Model([input_A, input_B, input_C], distance, name='siamese')

    return model


def parse_args(args):
    """
    Checks that input args are valid.
    """

    if args.save_weights_path is not None:
        if not os.path.isdir(args.save_weights_path):
            response = input("Save weights path does not exist. Create it? (Y/n) >> ")
            if response.lower() not in ["y", "yes", "1", ""]:
                exit()
            else:
                os.makedirs(args.save_weights_path)

    if args.metrics_path is not None:
        if not os.path.isdir(args.metrics_path):
            response = input("Metrics path does not exist. Create it? (Y/n) >> ")
            if response.lower() not in ["y", "yes", "1", ""]:
                exit()
            else:
                os.makedirs(args.metrics_path)


def main():

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(dest="data_path", metavar="DATA_PATH", help="Path to read examples from.")
    parser.add_argument("-sW", "--save_weights_path", metavar="SAVE_WEIGHTS_PATH", default=None, help="Path to save trained weights to. If no path is specified checkpoints are not saved.")
    parser.add_argument("-sM", "--save_model_path", metavar="SAVE_MODEL_PATH", default=None, help="Path to save trained model to.")
    parser.add_argument("-l", "--load_path", metavar="LOAD_PATH", default=None, help="Path to load trained model from. If no path is specified model is trained from scratch.")
    parser.add_argument("-m", "--metrics-path", metavar="METRICS_PATH", default=None, help="Path to save additional performance metrics to (for debugging purposes).")
    parser.add_argument("--PCA", metavar="PCA", default=False, help="If true, a PCA plot is saved.")
    parser.add_argument("--TSNE", metavar="TSNE", default=False, help="If true, a TSNE plot is saved.")
    parser.add_argument("--output_loss_threshold", metavar="OUTPUT_LOSS_THRESHOLD", default=None, help="Value between 0.0-1.0. Main function will return loss value of triplet at set percentage.")

    args = parser.parse_args()
    parse_args(args)

    X_shape, y_shape = utils.get_shapes(args.data_path, "train")

    # Build model
    input_shape = X_shape[1:]
    tower_model = build_tower_cnn_model(input_shape)  # single input model
    triplet_model = build_triplet_model(input_shape, tower_model)  # siamese model
    if args.load_path is not None:
        triplet_model.load_weights(args.load_path)

    # Setup callbacks for early stopping and model saving
    callback_list = setup_callbacks(args.save_weights_path)

    # Compile model
    adam = Adam(lr=LEARNING_RATE)
    triplet_model.compile(optimizer=adam, loss='mean_squared_error')
    tower_model.predict(np.zeros((1,) + input_shape))  # predict on some random data to activate predict()

    # Initializate online triplet generators
    training_batch_generator = OnlineTripletGenerator(args.data_path, "train", tower_model, batch_size=100)
    validation_batch_generator = OnlineTripletGenerator(args.data_path, "valid", tower_model, batch_size=100)

    triplet_model.fit_generator(generator=training_batch_generator, validation_data=validation_batch_generator,
                                callbacks=callback_list, epochs=EPOCHS)

    # Save weights
    if args.save_weights_path is not None:
        triplet_model.save_weights(args.save_weights_path + "final_weights.hdf5")

    # Save model
    if args.save_model_path is not None:
        tower_model.save(args.save_model_path + "tower_model.hdf5")
        triplet_model.save(args.save_model_path + "triplet_model.hdf5")

    """

    # Plot PCA/TSNE
    # For now, read all the valid anchors to do PCA
    # TODO: add function in util that reads a specified number of random samples from a dataset.
    if args.PCA is not False or args.TSNE is not False:
        X_valid_anchors, y_valid_anchors = utils.load_examples(args.data_path, "valid_anchors")
        X, Y = utils.shuffle_data(X_valid_anchors[:, :, :], y_valid_anchors[:, :], one_hot_labels=True)
        X = X[:5000, :, :]
        Y = Y[:5000, :]
        X = tower_model.predict(X)
        if args.PCA:
            utils.plot_with_PCA(X, Y)
        if args.TSNE:
            utils.plot_with_TSNE(X, Y)

    # Calculate loss value of triplet at a certain threshold
    if args.output_loss_threshold is not None:

        if not args.read_batches:  # Read all data at once

            # Load training triplets and validation triplets
            X_train_anchors, _ = utils.load_examples(args.data_path, "train_anchors")
            X_train_positives, _ = utils.load_examples(args.data_path, "train_positives")
            X_train_negatives, _ = utils.load_examples(args.data_path, "train_negatives")

            # Get abs(distance) of embeddings
            X_train = triplet_model.predict([X_train_anchors, X_train_positives, X_train_negatives])

        else:  # Read data in batches

            training_batch_generator = utils.DataGenerator(args.data_path, "train", batch_size=100, stop_after_batch=10)

            # Get abs(distance) of embeddings (one batch at a time)
            X_train = triplet_model.predict_generator(generator=training_batch_generator, verbose=1)

        X_train = np.sort(X_train, axis=None)
        print(X_train[int(float(args.output_loss_threshold) * X_train.shape[0])])

    """

    # Other things that may be useful later:

    # Evaluation function found in util.py of the Jiffy git repo
    # print(evaluate_test_embedding(train_embedding, y_train, test_embedding, y_test))

    # Training using fit_generator (probably not necessary for us because data is limited)
    # triplet_model.fit_generator(gen_batch(X_train, tr_trip_idxs, batch_size, dummy_y), epochs=1, steps_per_epoch=n_batches_per_epoch)


if __name__ == "__main__":
    main()