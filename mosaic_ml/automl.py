import random
import pynisher
import warnings
import pickle
import time
import os
import signal, psutil

from mosaic.scenario import ListTask, ComplexScenario, ChoiceScenario
from mosaic.mosaic import Search
from mosaic.env import Env

from mosaic_ml import model
from mosaic_ml.utils import balanced_accuracy, memory_limit, time_limit, TimeoutException

from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.exceptions import ConvergenceWarning
from sklearn.model_selection import StratifiedKFold
from sklearn.metrics import confusion_matrix
import numpy as np


from functools import partial

warnings.filterwarnings("ignore", category=ConvergenceWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", category=UserWarning)

LIST_TASK = [""]

class AutoML():
    def __init__(self, time_budget = None, time_limit_for_evaluation = None, training_log_file = "", info_training = {}, n_jobs = 1):
        self.time_budget = time_budget
        self.time_limit_for_evaluation = time_limit_for_evaluation
        self.training_log_file = training_log_file
        self.info_training = info_training
        self.n_jobs = n_jobs

    def configure_hyperparameter_space(self):
        if not hasattr(self, "X") or not hasattr(self, "y"):
            raise Exception("X, y is not defined.")

        # Data preprocessing
        list_data_preprocessing = model.get_all_data_preprocessing()
        data_preprocessing = []
        self.rules = []
        self.sampler = {}
        for dp_method in list_data_preprocessing:
            scenario, sampler_, rules_ = dp_method()
            data_preprocessing.append(scenario)
            self.rules.extend(rules_)
            self.sampler.update(sampler_)
        preprocessing = ChoiceScenario(name = "preprocessing", scenarios=data_preprocessing)

        # Classifier
        list_classifiers = model.get_all_classifier()
        classifiers = []
        for clf in list_classifiers:
            scenario, sampler_, rules_ = clf()
            classifiers.append(scenario)
            self.rules.extend(rules_)
            self.sampler.update(sampler_)
        classifier_model = ChoiceScenario(name = "classifier", scenarios=classifiers)

        # Pipeline = preprocessing + classifier
        self.start = ComplexScenario(name = "root", scenarios=[preprocessing, classifier_model], is_ordered=True)
        self.adjust_sampler()

    def adjust_sampler(self):
        for param in model.DATA_DEPENDANT_PARAMS:
            if param in self.sampler:
                self.sampler[param].value_list = [2, self.X.shape[1] - 1]
        for param in self.sampler:
            if param.endswith("__n_jobs"):
                self.sampler[param].value_list = self.n_jobs

    def upgrade_ressource(self, nb_ressource):
        for param in self.searcher.mcts.env.space.sampler:
            if param in model.Ressource_parameters:
                self.searcher.mcts.env.space.sampler[param].value_list = [nb_ressource, nb_ressource + 10]

    def fit(self, X, y):
        self.X = X
        self.y = y
        self.configure_hyperparameter_space()

        def kill_child_processes(parent_pid, sig=signal.SIGTERM):
            try:
                parent = psutil.Process(parent_pid)
            except psutil.NoSuchProcess:
                return
            children = parent.children(recursive=True)
            for process in children:
                process.send_signal(sig)

        def evaluate(config, bestconfig, X=None, y=None, info = {}, obj = None):
            print("\n#####################################################")
            preprocessing = None
            classifier = None

            for name, params in config:
                if name in model.list_available_preprocessing:
                    if name in ["LinearSVCPrep", "ExtraTreesClassifierPrep"]:
                        s, m = model.list_available_preprocessing[name]
                        preprocessing = s(m(**params))
                    else:
                        preprocessing = model.list_available_preprocessing[name](**params)
                if  name in model.list_available_classifiers:
                    classifier = model.list_available_classifiers[name](**params)

            if preprocessing is None or classifier is None:
                raise Exception("Classifier and/or Preprocessing not found\n {0}".format(config))

            pipeline = Pipeline(steps=[("preprocessing", preprocessing), ("classifier", classifier)])
            print(pipeline) # Print algo

            list_score = []
            try:
                skf = StratifiedKFold(n_splits=2)
                for train_index, valid_index in skf.split(X, y):
                    X_train, X_valid = X[train_index], X[valid_index]
                    y_train, y_valid = y[train_index], y[valid_index]

                    with time_limit(60):
                        searcher = pynisher.enforce_limits(mem_in_mb=12072, wall_time_in_s=60, cpu_time_in_s=60, grace_period_in_s = 2)(obj)
                        score = searcher(pipeline, X_train, y_train, X_valid, y_valid)
                        kill_child_processes(os.getpid())
                        del searcher
                        if score is None:
                            print("Run stopped ...")
                            return 0
                        elif score < bestconfig["score"]:
                            print(">>>>>>>>>>>>>>>> Score: {0} Current best score: {1}".format(score, bestconfig["score"]))
                            return score
                        else:
                            list_score.append(score)
            except TimeoutException as e:
                print("TimeoutException: {0}".format(e))
                return 0
            except ValueError as e:
                print("ValueError: {0}".format(e))
                return 0
            except TypeError as e:
                print("TypeError: {0}".format(e))
                return 0
            except AttributeError as e:
                print("AttributeError: {0}".format(e))
                return 0
            except pynisher.MemorylimitException as e:
                print("MemorylimitException: {0}".format(e))
                return 0
            except Exception as e:
                print("Exception: {0}".format(e))
                return 0

            score = min(list_score)
            if score > bestconfig["score"]:
                pickle.dump(pipeline, open(info["working_directory"] + str(time.time()) + ".pkl", "wb"))
                print(">>>>>>>>>>>>>>>> New best Score: {0}".format(score))
            return score

        def train_predict_func(model, X_train, y_train, X_valid, y_valid):
                taille = len(y_valid)
                model.fit(X_train, y_train)
                matrix_conf = confusion_matrix(y_valid, model.predict(X_valid))
                cost = [[0, 1, 2, 3, 4, 6],
                [1, 0, 1, 4, 5, 8],
                [3, 2, 0, 3, 5, 8],
                [10, 7, 5, 0, 2, 7],
                [20, 16, 12, 4, 0, 8],
                [44, 38, 32, 19, 13, 0]]
                final_score =  min([np.multiply(matrix_conf, cost).sum() / taille, 1])
                return 1 - final_score

        eval_func = partial(evaluate, X=self.X, y=self.y, info = self.info_training, obj = train_predict_func)

        self.searcher = Search(self.start, self.sampler, self.rules, eval_func, logfile = self.training_log_file)

        start_time = time.time()
        # self.upgrade_ressource(100)
        with time_limit(90000):
            self.searcher.run(nb_simulation = 100000000000, generate_image_path = self.info_training["images_directory"])
