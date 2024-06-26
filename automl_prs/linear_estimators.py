"""Linear models for use with two-step AutoML-PRS."""

import time
import logging
from pprint import pprint

import numpy as np
import pandas as pd
import polars as pl
from flaml.automl.model import SKLearnEstimator
from flaml import tune
from sklearn import linear_model
from sklearn.preprocessing import MinMaxScaler
from sklearn.base import clone
from sklearn.base import BaseEstimator, RegressorMixin, TransformerMixin
from tqdm import tqdm


SCALE_BY_DEFAULT = True


logger = logging.getLogger(__name__)


class CustomMinMaxScaler(TransformerMixin):
	"""MinMaxScaler to efficiently scale pandas or polars Dataframes."""

	def __init__(self):
		super().__init__()

		self.min_vals = None
		self.max_vals = None

	def fit(self, X):
		if isinstance(X, pd.DataFrame):
			self.min_vals = X.min(axis=0)
			self.max_vals = X.max(axis=0)
		elif isinstance(X, pl.DataFrame):
			self.min_vals = X.min().to_numpy()[0]
			self.max_vals = X.max().to_numpy()[0]

		return self
	
	def transform(self, X):
		assert self.min_vals is not None and self.max_vals is not None, (
			"fit() must be called before transform()"
		)

		if isinstance(X, pd.DataFrame):
			return (X - self.min_vals) / (self.max_vals - self.min_vals)
		elif isinstance(X, pl.DataFrame):
			# Subtract min_vals and divide by the range (max_vals - min_vals)
			# To do this in a no-copy manner, we use Polars' functionality
			# that allows operations directly on the DataFrame
			for column in X.columns:
				min_val = self.min_vals[X.columns.index(column)]
				max_val = self.max_vals[X.columns.index(column)]
				range_val = max_val - min_val
				if range_val == 0:
					scaled_column = X[column] - min_val
				else:
					scaled_column = (X[column] - min_val) / range_val

				# Use with_columns to update the column in-place
				X = X.with_columns(scaled_column.alias(column))
			return X
		
			
def subset_data(data, start, end, axis=0):
	if isinstance(data, (pd.DataFrame, pd.Series)):
		return data.iloc[start:end] if axis == 0 else data.iloc[:, start:end]	# type: ignore
	elif isinstance(data, pl.DataFrame):
		return data.slice(start, end - start)
	else:
		return data[start:end] if axis == 0 else data[:, start:end]
	

class PartitionedEnsembleRows(BaseEstimator, RegressorMixin):
	def __init__(self, estimator, n_partitions=3, verbose=1, **kwargs):
		self.estimator = estimator
		self.n_partitions = n_partitions
		self.verbose = verbose
		self.kwargs = kwargs
		self.models = []

	def fit(self, X, y):
		indices = np.arange(len(y))
		np.random.shuffle(indices)

		partition_size = len(y) // self.n_partitions

		for i in tqdm(
			range(self.n_partitions),
			desc='Fitting on sample partitions',
			total=self.n_partitions,
			ncols=100,
			disable=(self.verbose == 0)
		):
			start_idx = i * partition_size
			end_idx = (i + 1) * partition_size if i != self.n_partitions - 1 else len(y)
			X_subset = subset_data(X, start_idx, end_idx)
			y_subset = subset_data(y, start_idx, end_idx)

			model = clone(self.estimator)
			model.set_params(**self.kwargs)
			model.fit(X_subset, y_subset)
			self.models.append(model)

		return self

	def predict(self, X):
		predictions = np.zeros(len(X))

		for model in tqdm(
			self.models,
			desc='Predicting with all models',
			total=len(self.models),
			ncols=100,
			disable=(self.verbose == 0)
		):
			predictions += model.predict(X)

		return predictions / self.n_partitions


class ElasticNetEstimatorPRS(SKLearnEstimator):
	"""Elastic net estimator."""

	@classmethod
	def search_space(cls, data_size, task):
		space = {
			"alpha": {
				"domain": tune.loguniform(lower=1e-10, upper=2.0),
				"init_value": 1e-4,
			},
			"l1_ratio": {
				"domain": tune.uniform(0.0, 1),
				"init_value": 1.0,
			},
			"max_iter": {
				"domain": tune.lograndint(lower=800, upper=10000),
				"init_value": 2500,
				"low_cost_init_value": 800,
			},
			"tol": {
				"domain": tune.loguniform(lower=1e-8, upper=5e-3),
				"init_value": 1e-4,
			},
			"selection": {
				"domain": tune.choice(['cyclic', 'random']),
				"init_value": 'cyclic',
			}
		}
		return space

	def __init__(
			self,
			task="regression",
			n_jobs=None,
			scale=SCALE_BY_DEFAULT,
			**config
		):
		super().__init__(task, **config)
		
		if task != "regression":
			raise ValueError(
				"ElasticNetEstimatorPRS only supports regression tasks."
			)
		
		if scale:
			self.scaler = CustomMinMaxScaler()
			self.scaler_fit = False
		else:
			self.scaler = None
			self.scaler_fit = True
		
		self.estimator_class = linear_model.ElasticNet

	def _preprocess(self, X):
		"""Preprocess data, including scaling."""
		if self.scaler is not None and not self.scaler_fit:
			X = self.scaler.fit_transform(X)
			self.scaler_fit = True
		elif self.scaler is not None and self.scaler_fit:
			X = self.scaler.transform(X)
		
		return X
	
	def _fit(
		self,
		X_train,
		y_train,
		print_params=False,
		**kwargs
	):
		"""Fit the model."""
		if print_params:
			pprint(self.params)
		
		super()._fit(X_train, y_train, **kwargs)


class ElasticNetEstimatorMultiThreshPRS(ElasticNetEstimatorPRS):
	"""Elastic net estimator for AutoML-PRS with p-value and window
	size thresholds considered.
	"""

	def __init__(
		self,
		filter_threshold,
		task="regression",
		n_jobs=None,
		scale=SCALE_BY_DEFAULT,
		**config
	):
		super().__init__(task, **config)

		self.var_sets_map = None
		self.covar_cols = None
		self.filter_threshold = filter_threshold

	def _preprocess(self, X):
		"""Preprocess data by subsetting to variables included at threshold,
		then optional scaling."""
		# Get variant subset
		var_subset = self.var_sets_map[					# type: ignore
			self.filter_threshold
		]
		X = X[self.covar_cols + var_subset]

		if self.scaler is not None and not self.scaler_fit:
			X = self.scaler.fit_transform(X)
			self.scaler_fit = True
		elif self.scaler is not None and self.scaler_fit:
			X = self.scaler.transform(X)
		
		return X
	
	def _fit(
		self,
		X_train,
		y_train,
		var_sets_map,
		covar_cols,
		print_params=False,
		**kwargs
	):
		"""Fit the model."""
		if print_params:
			pprint(self.params)
			print(f"Filter threshold: {self.filter_threshold}", flush=True)

		# Update var_sets_map and covar_cols
		self.var_sets_map = var_sets_map
		self.covar_cols = covar_cols
		
		super()._fit(X_train, y_train, **kwargs)


class NPartElasticNetEstimatorPRS(ElasticNetEstimatorPRS):
	"""Elastic net estimator using n_partitions of the samples for
	memory reasons.
	"""

	def __init__(
		self,
		task="regression",
		n_partitions=2,
		n_jobs=None,
		scale=SCALE_BY_DEFAULT,
		**config
	):
		super().__init__(
			task,
			n_partitions=n_partitions,
			estimator=linear_model.ElasticNet(),
			**config
		)
		
		if task != "regression":
			raise ValueError(
				"ElasticNetEstimatorPRS only supports regression tasks."
			)
		
		if scale:
			self.scaler = CustomMinMaxScaler()
			self.scaler_fit = False
		else:
			self.scaler = None
			self.scaler_fit = True
		
		self.estimator_class = PartitionedEnsembleRows


class NPartElasticNetEstimatorMultiThreshPRS(NPartElasticNetEstimatorPRS):
	"""Elastic net estimator for AutoML-PRS with multiple p-value and window
	size thresholds considered, using n_partitions of the samples for
	memory reasons.
	"""

	def __init__(
		self,
		filter_threshold,
		task="regression",
		n_partitions=2,
		n_jobs=None,
		scale=SCALE_BY_DEFAULT,
		**config
	):
		super().__init__(
			task,
			n_partitions=n_partitions,
			**config
		)
		
		if task != "regression":
			raise ValueError(
				"ElasticNetEstimatorPRS only supports regression tasks."
			)
		
		if scale:
			self.scaler = CustomMinMaxScaler()
			self.scaler_fit = False
		else:
			self.scaler = None
			self.scaler_fit = True
		
		self.filter_threshold = filter_threshold
		self.estimator_class = PartitionedEnsembleRows

	def _preprocess(self, X):
		"""Preprocess data by subsetting to variables included at threshold,
		then optional scaling."""
		# Get variant subset
		var_subset = self.var_sets_map[					# type: ignore
			self.filter_threshold
		]
		X = X[self.covar_cols + var_subset]

		if self.scaler is not None and not self.scaler_fit:
			X = self.scaler.fit_transform(X)
			self.scaler_fit = True
		elif self.scaler is not None and self.scaler_fit:
			X = self.scaler.transform(X)
		
		return X
	
	def _fit(
		self,
		X_train,
		y_train,
		var_sets_map,
		covar_cols,
		print_params=False,
		**kwargs
	):
		"""Fit the model."""
		if print_params:
			pprint(self.params)
			print(f"Filter threshold: {self.filter_threshold}", flush=True)

		# Update var_sets_map and covar_cols
		self.var_sets_map = var_sets_map
		self.covar_cols = covar_cols
		
		super()._fit(X_train, y_train, **kwargs)


class SGDRegressorPRS(SKLearnEstimator):
	"""Stochastic gradient descent linear regression estimator."""

	@classmethod
	def search_space(cls, data_size, task):
		space = {
			"alpha": {
				"domain": tune.loguniform(lower=1e-10, upper=2.0),
				"init_value": 1e-4,
			},
			"l1_ratio": {
				"domain": tune.uniform(0.0, 1),
				"init_value": 1.0,
			},
			"n_iter_no_change": {
				"domain": tune.lograndint(lower=3, upper=100),
				"init_value": 10,
				"low_cost_init_value": 3,
			},
			"tol": {
				"domain": tune.loguniform(lower=1e-7, upper=5e-3),
				"init_value": 1e-3,
			},
			"learning_rate": {
				"domain": tune.choice(['optimal', 'invscaling', 'adaptive']),
				"init_value": 'invscaling',
			},
			"eta0": {
				"domain": tune.loguniform(lower=1e-7, upper=0.05),
				"init_value": 0.01,
			},
		}
		return space

	def __init__(
		self,
		task="regression",
		n_jobs=None,
		scale=SCALE_BY_DEFAULT,
		penalty="elasticnet",
		max_iter=10000,
		verbose=0,
		**config
	):
		super().__init__(
			task,
			penalty=penalty,
			verbose=verbose,
			early_stopping=True,
			max_iter=max_iter,
			**config
		)

		if task != "regression":
			raise ValueError(
				"SGDRegressorPRS only supports regression tasks."
			)
		
		if scale:
			self.scaler = CustomMinMaxScaler()
			self.scaler_fit = False
		else:
			self.scaler = None
			self.scaler_fit = True

		self.estimator_class = linear_model.SGDRegressor

	def _preprocess(self, X):
		"""Preprocess data, including scaling."""
		if self.scaler is not None and not self.scaler_fit:
			X = self.scaler.fit_transform(X)
			self.scaler_fit = True
		elif self.scaler is not None and self.scaler_fit:
			X = self.scaler.transform(X)
		
		return X
	
	def _fit(
		self,
		X_train,
		y_train,
		print_params=False,
		**kwargs
	):
		"""Fit the model."""
		if print_params:
			pprint(self.params)
		
		super()._fit(X_train, y_train, **kwargs)


class SGDRegressorMultiThreshPRS(SGDRegressorPRS):
	"""Stochastic gradient descent linear regression estimator for AutoML-PRS
	with p-value and window size thresholds considered.
	"""

	def __init__(
		self,
		filter_threshold,
		task="regression",
		n_jobs=None,
		scale=SCALE_BY_DEFAULT,
		penalty="elasticnet",
		max_iter=10000,
		verbose=0,
		**config
	):
		super().__init__(
			task,
			penalty=penalty,
			verbose=verbose,
			max_iter=max_iter,
			**config
		)
		
		if scale:
			self.scaler = CustomMinMaxScaler()
			self.scaler_fit = False
		else:
			self.scaler = None
			self.scaler_fit = True

		self.filter_threshold = filter_threshold
		self.estimator_class = linear_model.SGDRegressor

	def _preprocess(self, X):
		"""Preprocess data by subsetting to variables included at threshold,
		then optional scaling."""
		# Get variant subset
		var_subset = self.var_sets_map[					# type: ignore
			self.filter_threshold
		]
		X = X[self.covar_cols + var_subset]

		if self.scaler is not None and not self.scaler_fit:
			X = self.scaler.fit_transform(X)
			self.scaler_fit = True
		elif self.scaler is not None and self.scaler_fit:
			X = self.scaler.transform(X)
		
		return X
	
	def _fit(
		self,
		X_train,
		y_train,
		var_sets_map,
		covar_cols,
		print_params=False,
		**kwargs
	):
		"""Fit the model."""
		if print_params:
			pprint(self.params)
			print(f"Filter threshold: {self.filter_threshold}", flush=True)

		# Update var_sets_map and covar_cols
		self.var_sets_map = var_sets_map
		self.covar_cols = covar_cols
		
		super()._fit(X_train, y_train, **kwargs)
