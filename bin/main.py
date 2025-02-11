"""A medical image analysis pipeline.

The pipeline is used for brain tissue segmentation using a decision forest classifier.
"""
import argparse
import datetime
import os
import random
import sys
import timeit
import warnings
import numpy as np
import pylab as P
import seaborn as sns
import matplotlib.pyplot as plt

import SimpleITK as sitk
import sklearn.ensemble as sk_ensemble
import numpy as np
import pymia.data.conversion as conversion
import pymia.data.loading as load

sys.path.insert(0, os.path.join(os.path.dirname(sys.argv[0]), '..'))  # append the MIALab root directory to Python path
# fixes the ModuleNotFoundError when executing main.py in the console after code changes (e.g. git pull)
# somehow pip install does not keep track of packages

import mialab.data.structure as structure
import mialab.utilities.file_access_utilities as futil
import mialab.utilities.pipeline_utilities as putil

LOADING_KEYS = [structure.BrainImageTypes.T1w,
                structure.BrainImageTypes.T2w,
                structure.BrainImageTypes.GroundTruth,
                structure.BrainImageTypes.BrainMask,
                structure.BrainImageTypes.RegistrationTransform]  # the list of data we will load


def main(result_dir: str, data_atlas_dir: str, data_train_dir: str, data_test_dir: str):
    """Brain tissue segmentation using decision forests.

    The main routine executes the medical image analysis pipeline:

        - Image loading
        - Registration
        - Pre-processing
        - Feature extraction
        - Decision forest classifier model building
        - Segmentation using the decision forest classifier model on unseen images
        - Post-processing of the segmentation
        - Evaluation of the segmentation
    """
    seed = 42
    random.seed(seed)
    np.random.seed(seed)

    # load atlas images
    putil.load_atlas_images(data_atlas_dir)

    print('-' * 5, 'Training...')

    # crawl the training image directories
    crawler = load.FileSystemDataCrawler(data_train_dir,
                                         LOADING_KEYS,
                                         futil.BrainImageFilePathGenerator(),
                                         futil.DataDirectoryFilter())
    pre_process_params = {'skullstrip_pre': True,
                          'normalization_pre': False,
                          'artifact_pre': False,
                          'registration_pre': False,
                          'coordinates_feature': True,
                          'intensity_feature': True,
                          'gradient_intensity_feature': True}

    # STUDENT: initialize evaluate_BraTS, feature_mean_intensities and feature_std_intensities as global variables
    putil.init_global_variable()

    # STUDENT: parameters for execution
    plot_slice = False
    plot_hist = True
    putil.evaluate_BraTS = True  # only part of pipeline runnable if 'True': run in debug mode

    # STUDENT: choose normalization method
    #  'z':     Z-Score
    #  'ws':    White Stripe
    #  'hm':    Histogram Matching
    #  'fcm':   FCM White Matter Alignment
    norm_method = 'z'

    if not pre_process_params['normalization_pre']:
        norm_method = 'no'

    # STUDENT: choose artifact procedure
    # 'gaussian noise':     Gaussian Noise
    # 'zero frequencies':   Randomly selected frequencies are zero-filled
    artifact_method = 'zero frequencies'

    if not pre_process_params['artifact_pre']:
        artifact_method = 'none'

    # load images for training and pre-process
    images = putil.pre_process_batch(crawler.data, pre_process_params, norm_method=norm_method,
                                     artifact_method='none', multi_process=False)

    # STUDENT: plots for inspection
    if plot_slice is True:
        putil.plot_slice(images[0].images[structure.BrainImageTypes.T1w])

    if plot_hist is True:
        intensities_T1w, intensities_T2w = [], []
        nr_samples = 1
        for i in range(nr_samples):
            intensities_T1w.append(putil.get_masked_intensities(images[i].images[structure.BrainImageTypes.T1w],
                                   images[i].images[structure.BrainImageTypes.BrainMask]))
            intensities_T2w.append(putil.get_masked_intensities(images[i].images[structure.BrainImageTypes.T2w],
                                   images[i].images[structure.BrainImageTypes.BrainMask]))
        for i in range(nr_samples):
            plt.figure(1)
            sns.kdeplot(intensities_T1w[i])
            plt.figure(2)
            sns.kdeplot(intensities_T2w[i])
        for i in range(2):
            plt.figure(i+1)
            plt.xlabel('Intensity')
            plt.ylabel('PDF')
            # plt.title('Intensity density with ' + norm_method + ' normalization method')
            plt.ylabel('PDF')
            plt.savefig('./mia-result/plots/Result_Hist_norm-' + norm_method + '_T' + str(i+1) + 'w.png')
            plt.close()
            print('plot finished\n\n\n')

    # STUDENT: save preprocessed sitk images for visual inspection
    for i, img in enumerate(images):
        save_to_t1w = os.path.join('./mia-result/norm images/', norm_method + '-norm_' + images[i].id_ + '_T1w.nii.gz')
        save_to_t2w = os.path.join('./mia-result/norm images/', norm_method + '-norm_' + images[i].id_ + '_T2w.nii.gz')
        sitk.WriteImage(images[i].images[structure.BrainImageTypes.T1w], save_to_t1w)
        sitk.WriteImage(images[i].images[structure.BrainImageTypes.T1w], save_to_t2w)

    # STUDENT: print intensity means of feature images for pre evaluation
    mean = np.sum(putil.feature_mean_intensities, axis=0)/len(putil.feature_mean_intensities)
    std = np.sum(putil.feature_std_intensities, axis=0) / len(putil.feature_std_intensities)
    labels = ['White matter:', 'Grey matter:', 'Hippocampus:', 'Amygdala:   ', 'Thalamus:   ']
    weighting = ['T1w', 'T2w']
    print('\n----- Mean feature intensities in training -----')
    for w in range(2):
        print(weighting[w])
        for i in range(5):
            print(labels[i] + '\t' + str(mean[w, i]))
    print('\n----- Mean std of feature intensities in training -----')
    for w in range(2):
        print(weighting[w])
        for i in range(5):
            print(labels[i] + '\t' + str(std[w, i]))
    print('\n')
    putil.init_global_variable()  # reset mean and std lists for test procedure

    # generate feature matrix and label vector
    data_train = np.concatenate([img.feature_matrix[0] for img in images])
    labels_train = np.concatenate([img.feature_matrix[1] for img in images]).squeeze()

    # warnings.warn('Random forest parameters not properly set.')
    # we modified the number of decision trees in the forest to be 20 and the maximum tree depth to be 25
    # note, however, that these settings might not be the optimal ones...
    forest = sk_ensemble.RandomForestClassifier(max_features=images[0].feature_matrix[0].shape[1],
                                                n_estimators=20,  # 8 for low capacity, 20 for high capacity
                                                max_depth=25)  # 10 for low capacity, 25 for high capacity

    start_time = timeit.default_timer()
    forest.fit(data_train, labels_train)
    print(' Time elapsed:', timeit.default_timer() - start_time, 's')

    # create a result directory with timestamp
    t = datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S')
    result_dir = os.path.join(result_dir, t)
    os.makedirs(result_dir, exist_ok=True)

    print('-' * 5, 'Testing...')

    # initialize evaluator
    evaluator = putil.init_evaluator(result_dir)

    # crawl the training image directories
    crawler = load.FileSystemDataCrawler(data_test_dir,
                                         LOADING_KEYS,
                                         futil.BrainImageFilePathGenerator(),
                                         futil.DataDirectoryFilter())

    # load images for testing and pre-process
    pre_process_params['training'] = False
    images_test = putil.pre_process_batch(crawler.data, pre_process_params, norm_method=norm_method,
                                          artifact_method=artifact_method, multi_process=False)

    # STUDENT: print intensity means of feature images
    mean = np.sum(putil.feature_mean_intensities, axis=0) / len(putil.feature_mean_intensities)
    std = np.sum(putil.feature_std_intensities, axis=0) / len(putil.feature_std_intensities)
    labels = ['White matter:', 'Grey matter:', 'Hippocampus:', 'Amygdala:   ', 'Thalamus:   ']
    weighting = ['T1w', 'T2w']
    print('\n----- Mean feature intensities in testing -----')
    for w in range(2):
        print(weighting[w])
        for i in range(5):
            print(labels[i] + '\t' + str(mean[w, i]))
    print('\n----- Mean std of feature intensities in testing -----')
    for w in range(2):
        print(weighting[w])
        for i in range(5):
            print(labels[i] + '\t' + str(std[w, i]))
    print('\n')

    images_prediction = []
    images_probabilities = []

    for img in images_test:
        print('-' * 10, 'Testing', img.id_)

        start_time = timeit.default_timer()
        predictions = forest.predict(img.feature_matrix[0])
        probabilities = forest.predict_proba(img.feature_matrix[0])
        print(' Time elapsed:', timeit.default_timer() - start_time, 's')

        # convert prediction and probabilities back to SimpleITK images
        image_prediction = conversion.NumpySimpleITKImageBridge.convert(predictions.astype(np.uint8),
                                                                        img.image_properties)
        image_probabilities = conversion.NumpySimpleITKImageBridge.convert(probabilities, img.image_properties)

        # evaluate segmentation without post-processing
        evaluator.evaluate(image_prediction, img.images[structure.BrainImageTypes.GroundTruth], img.id_)

        images_prediction.append(image_prediction)
        images_probabilities.append(image_probabilities)

    # post-process segmentation and evaluate with post-processing
    post_process_params = {'simple_post': True}
    images_post_processed = putil.post_process_batch(images_test, images_prediction, images_probabilities,
                                                     post_process_params, multi_process=True)

    for i, img in enumerate(images_test):
        evaluator.evaluate(images_post_processed[i], img.images[structure.BrainImageTypes.GroundTruth],
                           img.id_ + '-PP')

        # save results
        sitk.WriteImage(images_prediction[i], os.path.join(result_dir, images_test[i].id_ + '_SEG.mha'), True)
        sitk.WriteImage(images_post_processed[i], os.path.join(result_dir, images_test[i].id_ + '_SEG-PP.mha'), True)


if __name__ == "__main__":
    """The program's entry point."""

    script_dir = os.path.dirname(sys.argv[0])

    parser = argparse.ArgumentParser(description='Medical image analysis pipeline for brain tissue segmentation')

    parser.add_argument(
        '--result_dir',
        type=str,
        default=os.path.normpath(os.path.join(script_dir, './mia-result')),
        help='Directory for results.'
    )

    parser.add_argument(
        '--data_atlas_dir',
        type=str,
        default=os.path.normpath(os.path.join(script_dir, '../data/atlas')),
        help='Directory with atlas data.'
    )

    parser.add_argument(
        '--data_train_dir',
        type=str,
        default=os.path.normpath(os.path.join(script_dir, '../data/train/')),
        help='Directory with training data.'
    )

    parser.add_argument(
        '--data_test_dir',
        type=str,
        default=os.path.normpath(os.path.join(script_dir, '../data/test/')),
        help='Directory with testing data.'
    )

    args = parser.parse_args()
    main(args.result_dir, args.data_atlas_dir, args.data_train_dir, args.data_test_dir)
