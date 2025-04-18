from __future__ import print_function, division
import os
import numpy as np
import pandas as pd
import pickle
import itertools
from sklearn.preprocessing import StandardScaler

import torch
from torch.utils.data import Dataset


class Generic_WSI_Survival_Dataset(Dataset):
	def __init__(self,
		df, print_info=False, n_bins=4, sign_path=False,
		indep_vars=[],  mode="omic", survival_time_list=[]):
		"""
		Args:
			print_info (bool): Flag to print dataset information.
			n_bins (int): Number of bins to split the survival time.
			proportional (bool): Flag to use proportional splitting of time intervals.
			
		"""
		self.print_info = print_info
		self.data_dir = None
		self.cluster_id_path = None
		self.num_intervals = n_bins
		self.mode = mode
		
		self.indep_vars = indep_vars
		if self.print_info:
			print("Number of selected tabular data: ", len(self.indep_vars))
		
		slide_data = df[["case_id", "slide_id", "survival_months", "censorship"]+self.indep_vars]
		
		patients_df = slide_data.drop_duplicates(['case_id']).copy()

		survival_time_list = survival_time_list if survival_time_list != [] else patients_df["survival_months"]
		_, time_breaks = pd.qcut(survival_time_list, q=self.num_intervals, retbins=True, labels=False)
		time_breaks[0] = 0
		time_breaks[-1] += 1
		self.time_breaks = time_breaks
		if self.print_info:
			print("Time intervals: ", self.time_breaks)

		self.patient_dict = {
			case: slide_data["slide_id"][slide_data["case_id"] == case].values \
			for case in slide_data["case_id"].unique()
			}
		
		disc_labels, _ = pd.cut(patients_df["survival_months"], bins=self.time_breaks, retbins=True, labels=False, right=False, include_lowest=True)
		patients_df.insert(2, 'label', disc_labels.values.astype(int))

		slide_data = patients_df
		slide_data.reset_index(drop=True, inplace=True)
		slide_data = slide_data.assign(slide_id=slide_data['case_id'])

		label_dict = {}
		key_count = 0
		for i in range(len(self.time_breaks)-1):
			for c in [0, 1]:
				label_dict.update({(i, c):key_count})
				key_count+=1

		self.label_dict = label_dict
		
		for i in slide_data.index:
			key = slide_data.loc[i, 'label']
			slide_data.at[i, 'disc_label'] = key
			censorship = slide_data.loc[i, 'censorship']
			key = (key, int(censorship))
			slide_data.at[i, 'label'] = label_dict[key]

		self.num_classes=len(self.label_dict)
		
		new_cols = list(slide_data.columns[-1:]) + list(slide_data.columns[:-1])
		slide_data = slide_data[new_cols]
		
		self.slide_data = slide_data.reset_index(drop=True)
		
		self.signatures = pd.read_csv(sign_path) if sign_path else None

		def series_intersection(s1, s2):
			return pd.Series(list(set(s1) & set(s2)))

		if self.signatures is not None:
			self.omic_names = []
			for col in self.signatures.columns:
				omic = self.signatures[col].dropna().unique()
				omic = np.concatenate([omic+mode for mode in ['_mut', '_cnv', '_rna', "_dna"]])
				omic = sorted(series_intersection(omic, self.indep_vars))
				self.omic_names.append(omic)
			self.omic_sizes = [len(omic) for omic in self.omic_names]
			self.indep_vars = list(np.unique(list(itertools.chain(*self.omic_names))))
			self.slide_data = self.slide_data[["case_id", "slide_id", "survival_months", "censorship", "disc_label", "label"]+self.indep_vars].reset_index(drop=True)
			print("Total Genetic Data:", len(self.indep_vars))
			if self.mode != "coattn":
				self.omic_sizes = len(self.indep_vars)
			else:
				print("OMIC SIZES:")
				for i in self.omic_sizes:
					print("\t", i)
		else:
			self.omic_sizes = len(self.indep_vars)
			self.omic_names = None

		if print_info:
			self.summarize()
			
	def getlabel(self, ids):
		return self.slide_data['label'][ids]
	
	def summarize(self):
		print("\n################## DATA SUMMARY ##########################")
		print("label column: {}".format("survival_months"))
		print("number of classes: {}".format(self.num_classes))
		for i in range(self.num_classes):
			cases = self.slide_data["case_id"][self.slide_data["label"]==i].values
			nb_cases = len(cases)
			nb_slides = sum([len(self.patient_dict[v]) for v in cases])
			print('Patient-LVL; Number of samples registered in class %d: %d' % (i, nb_cases))
			print('Slide-LVL; Number of samples registered in class %d: %d' % (i, nb_slides))
		print("########################################################\n")
		
	def __len__(self):
		return len(self.slide_data)

	def get_split_from_df(self, all_splits=None, split_key='train', scaler=None):
		if split_key == 'all':
			return Generic_Split(self.slide_data, self.time_breaks, self.indep_vars, self.mode, self.data_dir, self.cluster_id_path, patient_dict=self.patient_dict, print_info=self.print_info, num_classes=self.num_classes, signatures=self.signatures, omic_sizes=self.omic_sizes, omic_names=self.omic_names)
		split = all_splits[split_key]
		split = split.dropna().reset_index(drop=True)

		if len(split) > 0:
			mask = self.slide_data['slide_id'].isin(split.tolist())
			df_slice = self.slide_data[mask].reset_index(drop=True)
			split = Generic_Split(df_slice, self.time_breaks, self.indep_vars, self.mode, self.data_dir, self.cluster_id_path, patient_dict=self.patient_dict, print_info=self.print_info, num_classes=self.num_classes, signatures=self.signatures, omic_sizes=self.omic_sizes, omic_names=self.omic_names)
		else:
			split = None
		
		return split

	def return_splits(self, csv_path=None, return_all=False, stats_path=None):
		
		if return_all:
			test_split = self.get_split_from_df(split_key='all')
			if len(self.indep_vars) > 0:
				train_stats = pd.read_csv(stats_path)
				train_stats.set_index("Unnamed: 0", inplace=True)
				assert "mean" in train_stats.columns and "std" in train_stats.columns
				test_split.preprocess(train_stats, use_csv=True)
			return test_split
		all_splits = pd.read_csv(csv_path)
		train_split = self.get_split_from_df(all_splits=all_splits, split_key='train')
		val_split = self.get_split_from_df(all_splits=all_splits, split_key='val')
		test_split = self.get_split_from_df(all_splits=all_splits, split_key='test')
		
		train_stats = train_split.get_stats()
		sc = train_split.preprocess(train_stats)
		val_split.preprocess(train_stats, sc=sc)
		test_split.preprocess(train_stats, sc=sc)
		return (train_split, val_split, test_split), train_stats

	def __getitem__(self, idx):
		return None

	def apply_preprocessing(self, slide_data, stats):
		if slide_data.isna().any().any():
			print("Filling missing values with train medians:")
			for col_idx, col in enumerate(self.indep_vars):
				if col_idx % 10000 == 0:
					print("\tProcessing:", col_idx, "/", len(self.indep_vars))
				if slide_data[col].isna().any():
					slide_data[col] = slide_data[col].fillna(stats["median"].loc[col])

		print("Z-score normalization with train mean and std")
		print("\tBefore: {:.2f} - {:.2f}" .format(slide_data[self.indep_vars].min().min(), slide_data[self.indep_vars].max().max()))
		for col_idx, col in enumerate(self.indep_vars):
			slide_data[col] = (slide_data[col] - stats["mean"].loc[col]) / stats["std"].loc[col]
			
		print("\tAfter: {:.2f} - {:.2f}" .format(slide_data[self.indep_vars].min().min(), slide_data[self.indep_vars].max().max()))
		assert slide_data.isna().sum().sum() == 0, "There are still NaN values in the data."
		return slide_data


class MIL_Survival_Dataset(Generic_WSI_Survival_Dataset):
	def __init__(self, data_dir, cluster_id_path, **kwargs):
		super(MIL_Survival_Dataset, self).__init__(**kwargs)
		self.data_dir = data_dir
		self.cluster_id_path = cluster_id_path

	def __getitem__(self, idx):
		case_id = self.slide_data['case_id'][idx]
		label = torch.tensor(self.slide_data['disc_label'][idx])
		event_time = torch.tensor(self.slide_data["survival_months"][idx])
		c = torch.tensor(self.slide_data['censorship'][idx])
		slide_ids = self.patient_dict[case_id]
		
		if self.mode == 'coattn':
			path_features = []
			for slide_id in slide_ids:
				wsi_path = os.path.join(self.data_dir, '{}.pt'.format(slide_id.rstrip('.svs')))
				wsi_bag = torch.load(wsi_path, weights_only=True)
				path_features.append(wsi_bag)
			path_features = torch.cat(path_features, dim=0)
			omic1 = torch.tensor(self.slide_data[self.omic_names[0]].iloc[idx])
			omic2 = torch.tensor(self.slide_data[self.omic_names[1]].iloc[idx])
			omic3 = torch.tensor(self.slide_data[self.omic_names[2]].iloc[idx])
			omic4 = torch.tensor(self.slide_data[self.omic_names[3]].iloc[idx])
			omic5 = torch.tensor(self.slide_data[self.omic_names[4]].iloc[idx])
			omic6 = torch.tensor(self.slide_data[self.omic_names[5]].iloc[idx])
			
			return (path_features, omic1, omic2, omic3, omic4, omic5, omic6, label, event_time, c)
		
		if self.mode == 'cluster':
			path_features = []
			cluster_ids = []
			for slide_id in slide_ids:
				wsi_path = os.path.join(self.data_dir, '{}.pt'.format(slide_id.rstrip('.svs')))
				wsi_bag = torch.load(wsi_path, weights_only=True)
				path_features.append(wsi_bag)
				cluster_ids.extend(self.fname2ids[slide_id.rstrip('.svs')])
			path_features = torch.cat(path_features, dim=0)
			cluster_ids = torch.Tensor(cluster_ids)
			genomic_features = torch.tensor(self.slide_data[self.indep_vars].iloc[idx])
			
			return (cluster_ids, path_features, genomic_features, label, event_time, c)

		if "path" in self.mode:
			path_features = []
			for slide_id in slide_ids:
				wsi_path = os.path.join(self.data_dir, '{}.pt'.format(slide_id.rstrip('.svs')))
				wsi_bag = torch.load(wsi_path, weights_only=True)
				path_features.append(wsi_bag)
			path_features = torch.cat(path_features, dim=0)
		else:
			path_features = torch.zeros(1,)
		if 'omic' in self.mode:
			genomic_features = torch.tensor(self.slide_data[self.indep_vars].iloc[idx])
			
		else:
			genomic_features = torch.zeros(1,)
			
		return (path_features, genomic_features, label, event_time, c)


class Generic_Split(MIL_Survival_Dataset):
	def __init__(self, slide_data, time_breaks, indep_vars,
	mode, data_dir=None, cluster_id_path=None, patient_dict=None, 
	print_info=False, num_classes=4, signatures=None,
	omic_sizes=None, omic_names=None):
		"""
		Args:
			slide_data (DataFrame): Data for the current split.
			time_breaks (list): Time intervals for survival analysis.
			data_dir (string): Directory where the slide features are located.
			patient_dict (dict): Dictionary mapping patient IDs to slide data.
		"""
		self.slide_data = slide_data
		self.data_dir = data_dir
		self.cluster_id_path = cluster_id_path
		self.patient_dict = patient_dict
		self.time_breaks = time_breaks
		self.print_info = print_info
		if os.path.exists(cluster_id_path):
			with open(cluster_id_path, 'rb') as handle:
				self.fname2ids = pickle.load(handle)
		else:
			print("Cluster ID path not found.")

		self.slide_cls_ids = [[] for i in range(num_classes)]
		for i in range(num_classes):
			self.slide_cls_ids[i] = np.where(self.slide_data['label'] == i)[0]
		
		self.mode = mode
		self.indep_vars = indep_vars
		self.signatures = signatures

		self.omic_sizes = omic_sizes
		self.omic_names = omic_names
		
	def __len__(self):
		return len(self.slide_data)

	def get_stats(self):
		median_vals = self.slide_data[self.indep_vars].median()
		mean_vals = self.slide_data[self.indep_vars].mean()
		std_vals = self.slide_data[self.indep_vars].std()
		std_vals[std_vals == 0] = 1
		assert 0 not in std_vals.values, "There are still 0 values in the standard deviation."
		stats = pd.concat([median_vals, mean_vals, std_vals], axis=1)
		stats.columns = ['median', 'mean', 'std']
		return stats

	def preprocess(self, stats, sc=None, use_csv=False):
		if len(self.indep_vars) > 0:
			print("Filling missing values with train medians:")
			for col_idx, col in enumerate(self.indep_vars):
				if col_idx % 10000 == 0:
					print("\tProcessing:", col_idx, "/", len(self.indep_vars))
				if self.slide_data[col].isna().any():
					self.slide_data[col] = self.slide_data[col].fillna(stats["median"].loc[col])
			print("Z-score normalization with train mean and std")
			if sc == None and not use_csv:
				sc = StandardScaler()
				self.slide_data[self.indep_vars] = sc.fit_transform(self.slide_data[self.indep_vars])
				print(self.slide_data[self.indep_vars].max().max(), self.slide_data[self.indep_vars].min().min())
				return sc
			elif sc == None and use_csv:
				for col_idx, col in enumerate(self.indep_vars):
					mean_val = float(stats["mean"].loc[col])
					std_val = float(stats["std"].loc[col])
					self.slide_data[col] = (self.slide_data[col] - mean_val) / std_val
			else:
				self.slide_data[self.indep_vars] = sc.transform(self.slide_data[self.indep_vars])
			print(self.slide_data[self.indep_vars].max().max(), self.slide_data[self.indep_vars].min().min())
		assert self.slide_data.isna().sum().sum() == 0, "There are still NaN values in the data."
	