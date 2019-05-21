import os
import argparse

import numpy as np
from sklearn import svm
from keras.models import load_model, Model
from keras.layers import Input, Lambda
from keras.utils import CustomObjectScope
import keras.backend as K

import utils
import cnn_keras_siamese


def build_pair_distance_model(tower_model, input_shape):
    """
    Builds a model that takes a triplet as input and returns
    abs(A - P) and abs(A - N)
    """

    input_anchor = Input(input_shape)
    input_positive = Input(input_shape)
    input_negative = Input(input_shape)

    embedd_anchor = tower_model(input_anchor)
    embedd_positive = tower_model(input_positive)
    embedd_negative = tower_model(input_negative)

    tower_output_shape = tower_model.layers[-1].output_shape

    abs_difference = Lambda(lambda z: K.abs(z[0] - z[1]), output_shape=tower_output_shape)

    positive_pair_dist = abs_difference([embedd_anchor, embedd_positive])
    negative_pair_dist = abs_difference([embedd_anchor, embedd_negative])

    pair_distance_model = Model(inputs=[input_anchor, input_positive, input_negative], outputs=[positive_pair_dist, negative_pair_dist])

    return pair_distance_model


def shuffle(X, y):

    perm = np.random.permutation(X.shape[0])
    X = X[perm, :]
    y = y[perm]

    return X, y


def accuracy_FAR_FRR(y_true, y_pred):

    n_examples = y_true.shape[0]

    correct = 0
    FAR_errors = 0
    FRR_errors = 0
    for i in range(n_examples):

        if y_true[i] == y_pred[i]:
            correct += 1

        elif y_true[i] == 0 and y_pred[i] == 1:
            FAR_errors += 1

        elif y_true[i] == 1 and y_pred[i] == 0:
            FRR_errors += 1

    accuracy = float(correct) / n_examples
    FAR = float(FAR_errors) / (n_examples - np.sum(y_true))
    FRR = float(FRR_errors) / np.sum(y_true)

    return accuracy, FAR, FRR


def parse_args(args):
    """
    Checks that input args are valid.
    """

    assert os.path.isfile(args.triplets_path), "The specified triplet file does not exist."
    assert os.path.isfile(args.model_path), "The specified model file does not exist."

    if args.read_batches is not False:
        if args.read_batches.lower() in ("y", "yes", "1", "", "true", "t"):
            args.read_batches = True
        else:
            args.read_batches = False


def main():

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(dest="triplets_path", metavar="TRIPLETS_PATH", help="Path to read triplets from.")
    parser.add_argument(dest="model_path", metavar="MODEL_PATH", help="Path to read model from.")
    parser.add_argument("-b", "--read_batches", metavar="READ_BATCHES", default=False, help="If true, data is read incrementally in batches during training.")
    args = parser.parse_args()
    parse_args(args)

    # Load model
    with CustomObjectScope({'_euclidean_distance': cnn_keras_siamese._euclidean_distance,
                            'ALPHA': cnn_keras_siamese.ALPHA}):
        tower_model = load_model(args.model_path)
        tower_model.compile(optimizer='adam', loss='mean_squared_error')  # Model was previously not compiled

    X_shape, y_shape = utils.get_shapes(args.triplets_path, "train_anchors")

    # Build model to compute [A, P, N] => [abs(emb(A) - emb(P)), abs(emb(A) - emb(N))]
    pair_distance_model = build_pair_distance_model(tower_model, X_shape[1:])
    pair_distance_model.compile(optimizer="adam", loss="mean_squared_error")  # Need to compile in order to predict

    if not args.read_batches:  # Read all data at once

        # Load training triplets and validation triplets
        X_train_anchors, _ = utils.load_examples(args.triplets_path, "train_anchors")
        X_train_positives, _ = utils.load_examples(args.triplets_path, "train_positives")
        X_train_negatives, _ = utils.load_examples(args.triplets_path, "train_negatives")
        X_valid_anchors, _ = utils.load_examples(args.triplets_path, "valid_anchors")
        X_valid_positives, _ = utils.load_examples(args.triplets_path, "valid_positives")
        X_valid_negatives, _ = utils.load_examples(args.triplets_path, "valid_negatives")

        # Get abs(distance) of embeddings
        X_train_1, X_train_0 = pair_distance_model.predict([X_train_anchors, X_train_positives, X_train_negatives])
        X_valid_1, X_valid_0 = pair_distance_model.predict([X_valid_anchors, X_valid_positives, X_valid_negatives])

    else:  # Read data in batches

        training_batch_generator = utils.DataGenerator(args.triplets_path, "train", batch_size=100, shuffle=True, stop_after_batch=10)
        validation_batch_generator = utils.DataGenerator(args.triplets_path, "valid", batch_size=1000, shuffle=True)

        # Get abs(distance) of embeddings (one batch at a time)
        X_train_1, X_train_0 = pair_distance_model.predict_generator(generator=training_batch_generator, verbose=1)
        X_valid_1, X_valid_0 = pair_distance_model.predict_generator(generator=validation_batch_generator, verbose=1)

    # Stack positive and negative examples
    X_train = np.vstack((X_train_1, X_train_0))
    y_train = np.hstack((np.ones(X_train_1.shape[0], ), np.zeros(X_train_0.shape[0],)))
    X_valid = np.vstack((X_valid_1, X_valid_0))
    y_valid = np.hstack((np.ones(X_valid_1.shape[0], ), np.zeros(X_valid_0.shape[0],)))

    # Shuffle the data
    X_train, y_train = shuffle(X_train, y_train)
    X_valid, y_valid = shuffle(X_valid, y_valid)

    # Train SVM
    clf = svm.SVC(gamma='scale', verbose=True)
    clf.fit(X_train[:10000, :], y_train[:10000])

    # Evaluate SVM
    y_pred = clf.predict(X_valid)
    accuracy, FAR, FRR = accuracy_FAR_FRR(y_valid, y_pred)
    print("\n\n---- Validation Results ----")
    print("Accuracy = {}".format(accuracy))
    print("FAR = {}".format(FAR))
    print("FRR = {}".format(FRR))



if __name__ == "__main__":
    main()
