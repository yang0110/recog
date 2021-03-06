# -*- coding: utf-8 -*-

__author__ = 'kikohs'

import sys
import os
import time
import numpy as np
import scipy as sp
import scipy.sparse
import pandas as pd
import networkx as nx
import matplotlib as mpl
import matplotlib.pyplot as plt
import scipy.io

import itertools
import operator
import random
from collections import defaultdict, OrderedDict
import math


from sklearn.preprocessing import normalize
# project imports
import utils

import sklearn.decomposition.nmf as nmf


def soft_thresholding(data, value, substitute=0):
    mvalue = -value
    cond_less = np.less(data, value)
    cond_greater = np.greater(data, mvalue)
    data = np.where(cond_less & cond_greater, substitute, data)
    data = np.where(cond_less, data + value, data)
    data = np.where(cond_greater, data - value, data)
    return data


def create_recommendation_matrix(a_df, b_idx, gb_key, dataset_name, normalize=True, stop_criterion=1e-2, max_iter=1):
    # Create empty sparse matrix
    c = sp.sparse.dok_matrix((len(a_df), len(b_idx)), dtype=np.float64)
    # fill each playlist (row) with corresponding songs
    for i, (_, gb) in itertools.izip(itertools.count(), a_df.iterrows()):
        for b_id in gb[dataset_name + '_id']:
            # Get mapping form b_id to index position
            j = b_idx.get_loc(b_id)
            c[i, j] = 1.0
    # Convert to Compressed sparse row for speed
    c = sp.sparse.csr_matrix(c)
    if normalize:
        c = utils.create_double_stochastic_matrix(c, stop_criterion, max_iter)
    return c


def init_factor_matrices(nb_row, nb_col, rank, norm='l2'):
    a = np.random.random((nb_row, rank))
    b = np.random.random((rank, nb_col))

    a = normalize(a, norm=norm, axis=0)
    b = normalize(b, norm=norm, axis=1)
    return a, b


def graph_gradient_operator(g, key='weight'):
    k = sp.sparse.dok_matrix((g.number_of_edges(), g.number_of_nodes()))
    for i, (src, tgt, data) in itertools.izip(itertools.count(), g.edges_iter(data=True)):
        k[i, src] = data[key]
        k[i, tgt] = -data[key]
    return sp.sparse.csr_matrix(k)


def update_step(theta_tv_a, theta_tv_b, a, b, ka, norm_ka, kb, norm_kb, omega, oc, max_iter, method):
    b, a = update_factor(theta_tv_b, b, a, kb, norm_kb, omega, oc, max_iter, method)
    a, b = update_factor(theta_tv_a, a.T, b.T, ka, norm_ka, omega.T, oc.T, max_iter, method)
    a, b = a.T, b.T
    return a, b


def update_factor(theta_tv, X, Y, K, normK, omega, OC, nb_iter_max=300, method=0):
    # L2-norm of columns
    # X = (X.T / (np.linalg.norm(X, axis=1) + 1e-6)).T
    divider = np.linalg.norm(X, axis=1) + 1e-6
    X /= divider[:, np.newaxis]

    Y /= np.linalg.norm(Y, axis=0) + 1e-6

    # Primal variable
    Xb = X
    Xold = X
    # First dual variable
    P1 = Y.dot(X)
    # Second dual variable
    P2 = K.dot(X.T)

    # 2-norm largest singular value
    normY = sp.linalg.norm(Y, 2)
    # print 'norm Y:', normY

    # Primal-dual parameters
    gamma1 = 1e-1
    gamma2 = 1e-1

    # init time-steps
    sigma1 = 1.0 / normY
    tau1 = 1.0 / normY
    sigma2 = 1.0 / normK
    tau2 = 1.0 / normK

    stop = False
    nb_iter = 0

    # Precompute
    v = 4 * sigma1 * OC

    while not stop and nb_iter < nb_iter_max:
        # update P1 (NMF part)
        P1 += sigma1 * Y.dot(Xb)
        t = np.square(P1 - omega) + v
        P1 = 0.5 * (P1 + omega - np.sqrt(t))

        # update P2 (TV)
        P2 += sigma2 * K.dot(Xb.T)

        # TV
        if method == 0:
            P2 -= sigma2 * soft_thresholding(P2 / sigma2, theta_tv / sigma2)
        else:
            # Dirichlet
            P2 *= theta_tv / (theta_tv + sigma2)

        # new primal variable
        X = X - tau1 * (Y.T.dot(P1)) - tau2 * (K.T.dot(P2)).T

        # set negative values to 0 (element wise)
        X = np.maximum(X, 0)

        # # Acceleration, update time-steps
        # theta1 = 1. / np.sqrt(1 + 2 * gamma1 * tau1)
        # tau1 = tau1 * theta1
        # sigma1 = sigma1 / theta1
        # theta2 = 1. / np.sqrt(1 + 2 * gamma2 * tau2)
        # tau2 = tau2 * theta2
        # sigma2 = sigma2 / theta2

        # update primal variable for acceleration
        # t = X - Xold
        # Xb = X + (0.5 * theta1) * t + (0.5 * theta2) * t

        # TODO change ?
        # if theta1 == theta2 == 1
        theta1 = 1.0
        theta2 = 1.0
        Xb = 2 * X - Xold

        # update Xold
        Xold = X
        nb_iter += 1

    return X, Y


def proximal_training(C, WA, WB, rank, Obs=None,
                      theta_tv_a=100,
                      theta_tv_b=0.01,
                      max_outer_iter=7,
                      max_inner_iter=800,
                      A=None, B=None, data_path=None,
                      load_from_disk=False, validation_func=None, random_init=False, verbose=0, method=0):
    start = time.time()
    GA = utils.convert_adjacency_matrix(WA)
    GB = utils.convert_adjacency_matrix(WB)

    if load_from_disk and data_path is not None:
        data = np.load(data_path + '.npz')
        A = data['A']
        B = data['B']
    else:
        if random_init:
            A, B = init_factor_matrices(C.shape[0], C.shape[1], rank)
        else:
            if A is None or B is None:
                A, B = nmf._initialize_nmf(C, rank, None)

    KA = graph_gradient_operator(GA)
    KB = graph_gradient_operator(GB)

    # For sparse matrix
    _, normKA, _ = sp.sparse.linalg.svds(KA, 1)
    _, normKB, _ = sp.sparse.linalg.svds(KB, 1)
    normKA = normKA[0]
    normKB = normKB[0]

    if Obs is None:  # no observation mask
        Obs = 0.1 * np.ones(C.shape)
        mask = C > 0
        if isinstance(C, sp.sparse.base.spmatrix):
            mask = mask.toarray()
        Obs[mask] = 1.0
        Obs = np.array(Obs)

    # Mask over rating matrix, computed once
    OC = C.toarray()

    stop = False
    nb_iter = 0
    error = False
    while not stop and nb_iter < max_outer_iter:
        tick = time.time()
        A, B = update_step(theta_tv_a, theta_tv_b, A, B, KA, normKA, KB, normKB, Obs, OC, max_inner_iter, method)
        nb_iter += 1

        if data_path is not None:
            np.savez(data_path, A=A, B=B, theta_tv_a=theta_tv_a, theta_tv_b=theta_tv_b)

        if verbose > 0:
            if validation_func is not None:
                t = validation_func(np.array(A), np.array(B))
                print t
                print t.mean()
                sys.stdout.flush()
                # utils.plot_factor_mat(A, 'A step' + str(nb_iter))

            print('Step:{} done in {} seconds\n'.format(nb_iter, time.time() - tick))

    if not error:
            print 'Max iterations reached', nb_iter, 'steps,', \
                'reconstruction error:', sp.linalg.norm(C - A.dot(B))
    else:
        print 'Error: try to increase min_iter_inner, the number of iteration for the inner loop'

    print 'Total elapsed time:', time.time() - start, 'seconds'
    return np.array(A), np.array(B)


def recommend_from_keypoints(A, B, keypoints, k, idmap=None, threshold=1e-10, knn_A=50):
    """Keypoints: list of tuple (movie, rating) or (song, rating), idmap: if given maps idspace to index in matrix"""
    rank = B.shape[0]
    length = B.shape[1]

    # mask = np.zeros(length)
    mask = np.ones(length) * 0.1
    if idmap is not None:
        mask_idx = map(lambda x: idmap[x[0]], keypoints)
    else:
        mask_idx = map(lambda x: x[0], keypoints)
    mask[mask_idx] = 1.0
    mask = np.diag(mask)

    ratings = np.zeros(length)
    ratings[mask_idx] = map(lambda x: x[1], keypoints)

    z = B.dot(mask).dot(B.T) + 2e-2 * np.eye(rank)
    q = B.dot(mask).dot(ratings)

    # Results
    row_a = sp.linalg.solve(z, q)
    z = np.linalg.norm(A - row_a, axis=1)

    sigma = np.mean(z) / 4.0
    w = np.exp(-np.square(z) / (sigma * sigma))

    # Pick knn Best
    idx = np.argsort(w)
    nb_elems = min(knn_A, len(w))
    top_w = w[idx][-nb_elems:]
    top_idx = idx[-nb_elems:]

    # row size multiplication (new solution), weighted sum
    row_a = np.sum(np.multiply(A[top_idx],  top_w[:, np.newaxis]), axis=0) / np.sum(top_w)
    # Multiply estimated C by B
    raw = np.array(row_a.dot(B))
    # Filter numeric errors
    mask = raw > threshold
    points = raw[mask]
    nb_elems = min(k, len(points))
    # Get valid subset of songs
    position = np.arange(len(mask))[mask]
    # Get unsorted subset index of k highest values
    ind = np.argpartition(points, -nb_elems)[-nb_elems:]
    # Get sorted subset index of highest values, sorted by high values
    ind = ind[np.argsort(points[ind])][::-1]
    # map subset index to global position of k elements of highest value
    elems = position[ind]
    return elems, raw
