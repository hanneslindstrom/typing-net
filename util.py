import random
import os
import h5py

import numpy as np


def load_examples(data_path, dataset_name):
    """
    Loads the following datasets from data_path:

    X_train, y_train - Training data with only authorized users.
    X_valid, y_valid - Validation data with only authorized users.
    X_test_valid, y_test_valid - Test data with valid (i.e. authorized) users.
    X_test_unknown, y_test_unknown - Test data with unknown (i.e. unauthorized) users.

    These datasets can be generated with the script generate_examples.py

    Returns:
    Matrices X_{type} of shape (#examples, example_length, feature_length)
    Matrices y_{type} of shape (#examples, #users)
    """

    if not os.path.isfile(data_path):
        print("The file {} does not exist".format(data_path))
        exit()

    data_file = h5py.File(data_path, "r")
    X = data_file["X_" + dataset_name][()]
    y = data_file["y_" + dataset_name][()]

    return X, y


def shuffle_data(X, y):
    """
    Shuffles the data in X, y with the same random permutation.
    """

    n_examples = X.shape[0]

    perm = np.random.permutation(n_examples)
    X = X[perm, :, :]
    y = y[perm, :]

    return X, y


def split_data(X, y, train_frac, valid_frac, test_frac, shuffle=True):
    """
    Splits data into train/valid/test-sets according to the specified fractions.
    If shuffle is True, data is shuffled before splitting.
    """

    np.random.seed(1)

    assert train_frac + valid_frac + test_frac == 1, "Train/valid/test data fractions do not sum to one"

    n_examples = X.shape[0]

    # Shuffle
    if shuffle:
        X, y = shuffle_data(X, y)

    # Split
    ind_1 = int(np.round(train_frac*n_examples))
    ind_2 = int(np.round(ind_1 + valid_frac*n_examples))

    X_train = X[0:ind_1, :, :]
    y_train = y[0:ind_1, :]
    X_valid = X[ind_1:ind_2, :, :]
    y_valid = y[ind_1:ind_2, :]
    X_test = X[ind_2:, :, :]
    y_test = y[ind_2:, :]

    assert X_train.shape[0] + X_valid.shape[0] + X_test.shape[0] == n_examples, "Data split failed"

    return (X_train, y_train, X_valid, y_valid, X_test, y_test)


def index_to_one_hot(y, n_classes):
    """
    Converts a list of indices to one-hot encoding.
    Example: y = [1, 0, 3] => np.array([[0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 1]])

    If a label is -1 (unknown), its one-hot enoding becomes [-1, ..., -1]
    """

    if y.size == 0:
        return y

    minus_one = np.where(y == -1)
    y = y.reshape(-1)
    one_hot = np.eye(n_classes)[y]
    one_hot[minus_one, :] = -np.ones((n_classes,))

    return one_hot


def one_hot_to_index(y):
    """
    Converts numpy array of one-hot encodings to list of indices.
    Example: y = np.array([[0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 1]]) => [1, 0, 3]
    """
    if len(y.shape) == 1 or y.shape[1] == 0:
        if np.nonzero(y)[0].size == 0:
            return -1
        else:
            return np.argmax(y)

    indices = []
    for num in y:
        if np.nonzero(num)[0].size == 0:
            indices.append(-1)
        else:
            indices.append(np.argmax(num))

    return indices


def split_per_user(X, y, train_frac, valid_frac, test_frac, shuffle=False):
    """
    Splits the data into train/valid/test while still ensuring that each
    set has class balance. Does NOT shuffle the data before splitting by default.
    """

    np.random.seed(1)

    assert train_frac + valid_frac + test_frac == 1, "Train/valid/test data fractions do not sum to one"

    # Shuffle
    if shuffle:
        X, y = shuffle_data(X, y)

    n_users = y.shape[1]

    X_train = np.empty((0,) + X.shape[1:])
    y_train = np.empty((0,) + y.shape[1:])
    X_valid = np.empty((0,) + X.shape[1:])
    y_valid = np.empty((0,) + y.shape[1:])
    X_test = np.empty((0,) + X.shape[1:])
    y_test = np.empty((0,) + y.shape[1:])

    for user in range(n_users):
        user_inds = np.where(y[:, user] == 1)[0]
        X_train_sub, y_train_sub, X_valid_sub, y_valid_sub, X_test_sub, y_test_sub = split_data(X[user_inds, :, :], y[user_inds, :],
                                                                                                train_frac, valid_frac, test_frac, shuffle=False)
        X_train = np.vstack((X_train, X_train_sub)) if X_train.size else X_train_sub
        y_train = np.vstack((y_train, y_train_sub)) if y_train.size else y_train_sub
        X_valid = np.vstack((X_valid, X_valid_sub)) if X_valid.size else X_valid_sub
        y_valid = np.vstack((y_valid, y_valid_sub)) if y_valid.size else y_valid_sub
        X_test = np.vstack((X_test, X_test_sub)) if X_test.size else X_test_sub
        y_test = np.vstack((y_test, y_test_sub)) if y_test.size else y_test_sub

    return X_train, y_train, X_valid, y_valid, X_test, y_test


def split_on_users(X, y, n_valid_users, pick_random=False, add_other=False, n_invalid_users=None):
    """
    If add_other is False (default):

    Splits the given dataset into two sets:
    X_valid, y_valid - Data from the set of n_valid_users random users that are authorized.
    X_unknown, y_unknown - Data from the remaining set of users

    Data is relabeled as one-hot for valid users, and [-1, ..., -1] for unknown users.

    -------------------------------------------------------------------------------------------------

    If add_other is True (used to split data for "valid-plus-others" approach):

    n_invalid_users must be specified.

    Splits the given dataset into three sets:
    X_valid, y_valid - Data from the set of n_valid_users that are authorized.
    X_invalid, y_invalid - Data from a set of n_invalid_users that are known to be unauthorized.
    X_unknown, y_unknown - Data from users that are unauthorized, but never gets seen during training.

    Data is relabeled as one-hot with dimension (n_valid_users + 1) for valid and invalid users
    where last index represents "others". Data is relabeled with [-1, ..., -1] for unknown users.

    --------------------------------------------------------------------------------------------------

    Other options:

    - If pick_random is true, sets of users are selected randomly. Otherwise first users are selected.

    """

    n_examples, n_users = y.shape

    if add_other:
        assert n_invalid_users is not None, "Argument n_invalid_users must be specified when add_other is True."
        assert n_valid_users + n_invalid_users <= n_users, "Number of valid/invalid users specified exceeds the total number of users."
    else:
        assert n_valid_users <= n_users, "Number of valid users specified exceeds the total number of users."

    if pick_random:
        valid_users = random.sample(range(n_users), k=n_valid_users)
    else:
        valid_users = range(n_valid_users)

    if add_other:
        remaining_users = [user for user in range(n_users) if user not in valid_users]
        if pick_random:
            invalid_users = random.sample(remaining_users, k=n_invalid_users)
        else:
            invalid_users = range(n_valid_users, n_valid_users + n_invalid_users)

    X_valid, y_valid = [], []
    X_unknown, y_unknown = [], []
    if add_other:
        X_invalid, y_invalid = [], []

    for i in range(n_examples):
        user = np.asscalar(np.where(y[i, :] == 1)[0])
        if user in valid_users:
            X_valid.append(X[i, :])
            y_valid.append(valid_users.index(user))
        elif add_other:
            if user in invalid_users:
                X_invalid.append(X[i, :])
                y_invalid.append(n_valid_users)
            else:
                X_unknown.append(X[i, :])
                y_unknown.append(n_valid_users)
        else:
            X_unknown.append(X[i, :])
            y_unknown.append(-1)

    X_valid, y_valid = np.asarray(X_valid), np.asarray(y_valid)
    X_unknown, y_unknown = np.asarray(X_unknown), np.asarray(y_unknown)
    if add_other:
        X_invalid, y_invalid = np.asarray(X_invalid), np.asarray(y_invalid)

    if add_other:
        y_valid = index_to_one_hot(y_valid, n_valid_users + 1)
        y_unknown = index_to_one_hot(y_unknown, n_valid_users + 1)
        y_invalid = index_to_one_hot(y_invalid, n_valid_users + 1)
    else:
        y_valid = index_to_one_hot(y_valid, n_valid_users)
        y_unknown = index_to_one_hot(y_unknown, n_valid_users)

    if add_other:
        return X_valid, y_valid, X_invalid, y_invalid, X_unknown, y_unknown

    return X_valid, y_valid, X_unknown, y_unknown
