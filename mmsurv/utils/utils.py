import os
import math
import pandas as pd
from itertools import islice, chain
import collections
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Sampler, WeightedRandomSampler, RandomSampler, SequentialSampler, sampler
import torch.optim as optim
device=torch.device("cuda" if torch.cuda.is_available() else "cpu")

def get_data(args):
	df = pd.read_csv(args.csv_path, compression="zip" if ".zip" in args.csv_path else None)
	
	indep_vars = []
	if args.omics not in ["None", "none", None]:
		print("Selected omics variables:")
		if args.selected_features:
			omics_cols = {k: [col for col in df.columns if col[-3:]==k] for k in args.omics.split(",")}
			indep_vars = list(chain(*omics_cols.values()))
			for k, v in omics_cols.items():
				print("\t", k, len(v))
		else:
			remove_cols = {k: [col for col in df.columns if col[-3:]==k] for k in ["cli", "cnv", "rna", "pro", "mut", "dna"]}
			if "cli" in args.omics:
				cli_cols = remove_cols.pop("cli")
				print("\tcli", len(cli_cols))
				indep_vars.extend(cli_cols)
			df = df[[i for i in df.columns if i not in list(chain(*remove_cols.values()))]]
			print(df.shape)
			for g in args.omics.split(","):
				if g != "cli":
					gen_df = pd.read_csv(f"{args.dataset_dir}/{args.data_name}_{g}.csv.zip", compression="zip")
					indep_vars.extend(gen_df.columns[1:])
					print("\t", g, gen_df.shape[1]-1)
					df = pd.merge(df, gen_df, on='case_id', how="outer")
			df = df.reset_index(drop=True).drop(df.index[df["event"].isna()]).reset_index(drop=True)
	args.nb_tabular_data = len(indep_vars)
	
	print("Total number of cases: {} | slides: {}" .format(len(df["case_id"].unique()), len(df)))
	return df, indep_vars


class SubsetSequentialSampler(Sampler):
	"""Samples elements sequentially from a given list of indices, without replacement.

	Arguments:
		indices (sequence): a sequence of indices
	"""
	def __init__(self, indices):
		self.indices = indices

	def __iter__(self):
		return iter(self.indices)

	def __len__(self):
		return len(self.indices)

def collate_MIL(batch):
	img = torch.cat([item[0] for item in batch], dim = 0)
	label = torch.LongTensor([item[1] for item in batch])
	return [img, label]

def collate_features(batch):
	img = torch.cat([item[0] for item in batch], dim = 0)
	coords = np.vstack([item[1] for item in batch])
	return [img, coords]

def collate_MIL_survival(batch):
	img = torch.cat([item[0] for item in batch], dim = 0)
	omic = torch.cat([item[1] for item in batch], dim = 0).type(torch.FloatTensor)
	label = torch.LongTensor(np.array([item[2] for item in batch]))
	event_time = torch.FloatTensor([item[3] for item in batch])
	c = torch.FloatTensor([item[4] for item in batch])
	return [img, omic, label, event_time, c]

def collate_MIL_survival_cluster(batch):
	img = torch.cat([item[1] for item in batch], dim = 0)
	cluster_ids = torch.cat([item[0] for item in batch], dim = 0).type(torch.LongTensor)
	omic = torch.cat([item[2] for item in batch], dim = 0).type(torch.FloatTensor)
	label = torch.LongTensor(np.array([item[3] for item in batch]))
	event_time = torch.FloatTensor([item[4] for item in batch])
	c = torch.FloatTensor([item[5] for item in batch])
	return [cluster_ids, img, omic, label, event_time, c]

def collate_MIL_survival_sig(batch):
	img = torch.cat([item[0] for item in batch], dim = 0)
	omic1 = torch.cat([item[1] for item in batch], dim = 0).type(torch.FloatTensor)
	omic2 = torch.cat([item[2] for item in batch], dim = 0).type(torch.FloatTensor)
	omic3 = torch.cat([item[3] for item in batch], dim = 0).type(torch.FloatTensor)
	omic4 = torch.cat([item[4] for item in batch], dim = 0).type(torch.FloatTensor)
	omic5 = torch.cat([item[5] for item in batch], dim = 0).type(torch.FloatTensor)
	omic6 = torch.cat([item[6] for item in batch], dim = 0).type(torch.FloatTensor)

	label = torch.LongTensor(np.array([item[7] for item in batch]))
	event_time = torch.FloatTensor([item[8] for item in batch])
	c = torch.FloatTensor([item[9] for item in batch])
	return [img, omic1, omic2, omic3, omic4, omic5, omic6, label, event_time, c]

def get_simple_loader(dataset, batch_size=1):
	kwargs = {'num_workers': 4} if device.type == "cuda" else {}
	loader = DataLoader(dataset, batch_size=batch_size, sampler = sampler.SequentialSampler(dataset), collate_fn = collate_MIL, **kwargs)
	return loader 

def get_split_loader(split_dataset, training = False, testing = False, weighted = False, mode='coattn', batch_size=1):
	"""
		return either the validation loader or training loader 
	"""
	if mode == 'coattn':
		collate = collate_MIL_survival_sig
	elif mode == 'cluster':
		collate = collate_MIL_survival_cluster
	else:
		collate = collate_MIL_survival
	
	kwargs = {'num_workers': 4} if device.type == "cuda" else {}
	if not testing:
		if training:
			if weighted:
				weights = make_weights_for_balanced_classes_split(split_dataset)
				loader = DataLoader(split_dataset, batch_size=batch_size, sampler = WeightedRandomSampler(weights, len(weights)), collate_fn = collate, **kwargs)    
			else:
				loader = DataLoader(split_dataset, batch_size=batch_size, sampler = RandomSampler(split_dataset), collate_fn = collate, **kwargs)
		else:
			loader = DataLoader(split_dataset, batch_size=batch_size, sampler = SequentialSampler(split_dataset), collate_fn = collate, **kwargs)
	
	else:
		ids = np.random.choice(np.arange(len(split_dataset), int(len(split_dataset)*0.1)), replace = False)
		loader = DataLoader(split_dataset, batch_size=1, sampler = SubsetSequentialSampler(ids), collate_fn = collate, **kwargs )

	return loader

def get_optim(model, args):
	if args.opt == "adam":
		optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr, weight_decay=args.reg)
	elif args.opt == 'sgd':
		optimizer = optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr, momentum=0.9, weight_decay=args.reg)
	else:
		raise NotImplementedError
	return optimizer

def print_network(net):
	num_params = 0
	num_params_train = 0
	print(net)
	
	for param in net.parameters():
		n = param.numel()
		num_params += n
		if param.requires_grad:
			num_params_train += n
	
	print('Total number of parameters: %d' % num_params)
	print('Total number of trainable parameters: %d' % num_params_train)


def generate_split(cls_ids, val_num, test_num, samples, n_splits = 5,
	seed = 7, label_frac = 1.0, custom_test_ids = None):
	indices = np.arange(samples).astype(int)
	
	if custom_test_ids is not None:
		indices = np.setdiff1d(indices, custom_test_ids)

	np.random.seed(seed)
	for i in range(n_splits):
		all_val_ids = []
		all_test_ids = []
		sampled_train_ids = []
		
		if custom_test_ids is not None: # pre-built test split, do not need to sample
			all_test_ids.extend(custom_test_ids)

		for c in range(len(val_num)):
			possible_indices = np.intersect1d(cls_ids[c], indices) #all indices of this class
			remaining_ids = possible_indices

			if val_num[c] > 0:
				val_ids = np.random.choice(possible_indices, val_num[c], replace = False) # validation ids
				remaining_ids = np.setdiff1d(possible_indices, val_ids) #indices of this class left after validation
				all_val_ids.extend(val_ids)

			if custom_test_ids is None and test_num[c] > 0: # sample test split

				test_ids = np.random.choice(remaining_ids, test_num[c], replace = False)
				remaining_ids = np.setdiff1d(remaining_ids, test_ids)
				all_test_ids.extend(test_ids)

			if label_frac == 1:
				sampled_train_ids.extend(remaining_ids)
			
			else:
				sample_num  = math.ceil(len(remaining_ids) * label_frac)
				slice_ids = np.arange(sample_num)
				sampled_train_ids.extend(remaining_ids[slice_ids])

		yield sorted(sampled_train_ids), sorted(all_val_ids), sorted(all_test_ids)


def nth(iterator, n, default=None):
	if n is None:
		return collections.deque(iterator, maxlen=0)
	else:
		return next(islice(iterator,n, None), default)

def calculate_error(Y_hat, Y):
	error = 1. - Y_hat.float().eq(Y.float()).float().mean().item()

	return error

def make_weights_for_balanced_classes_split(dataset):
	N = float(len(dataset))                                           
	weight_per_class = [N/len(dataset.slide_cls_ids[c]) for c in range(len(dataset.slide_cls_ids))]                                                                                                     
	weight = [0] * int(N)                                           
	for idx in range(len(dataset)):   
		y = dataset.getlabel(idx)                        
		weight[idx] = weight_per_class[y]                                  

	return torch.DoubleTensor(weight)

def initialize_weights(module):
	for m in module.modules():
		if isinstance(m, nn.Linear):
			nn.init.xavier_normal_(m.weight)
			m.bias.data.zero_()
		
		elif isinstance(m, nn.BatchNorm1d):
			nn.init.constant_(m.weight, 1)
			nn.init.constant_(m.bias, 0)


def dfs_freeze(model):
	for name, child in model.named_children():
		for param in child.parameters():
			param.requires_grad = False
		dfs_freeze(child)


def dfs_unfreeze(model):
	for name, child in model.named_children():
		for param in child.parameters():
			param.requires_grad = True
		dfs_unfreeze(child)


# divide continuous time scale into k discrete bins in total,  T_cont \in {[0, a_1), [a_1, a_2), ...., [a_(k-1), inf)}
# Y = T_discrete is the discrete event time:
# Y = 0 if T_cont \in (-inf, 0), Y = 1 if T_cont \in [0, a_1),  Y = 2 if T_cont in [a_1, a_2), ..., Y = k if T_cont in [a_(k-1), inf)
# discrete hazards: discrete probability of h(t) = P(Y=t | Y>=t, X),  t = 0,1,2,...,k
# S: survival function: P(Y > t | X)
# all patients are alive from (-inf, 0) by definition, so P(Y=0) = 0
# h(0) = 0 ---> do not need to model
# S(0) = P(Y > 0 | X) = 1 ----> do not need to model
'''
Summary: neural network is hazard probability function, h(t) for t = 1,2,...,k
corresponding Y = 1, ..., k. h(t) represents the probability that patient dies in [0, a_1), [a_1, a_2), ..., [a_(k-1), inf]
'''
# def neg_likelihood_loss(hazards, Y, c):
#   batch_size = len(Y)
#   Y = Y.view(batch_size, 1) # ground truth bin, 1,2,...,k
#   c = c.view(batch_size, 1).float() #censorship status, 0 or 1
#   S = torch.cumprod(1 - hazards, dim=1) # surival is cumulative product of 1 - hazards
#   # without padding, S(1) = S[0], h(1) = h[0]
#   S_padded = torch.cat([torch.ones_like(c), S], 1) #S(0) = 1, all patients are alive from (-inf, 0) by definition
#   # after padding, S(0) = S[0], S(1) = S[1], etc, h(1) = h[0]
#   #h[y] = h(1)
#   #S[1] = S(1)
#   neg_l = - c * torch.log(torch.gather(S_padded, 1, Y)) - (1 - c) * (torch.log(torch.gather(S_padded, 1, Y-1)) + torch.log(hazards[:, Y-1]))
#   neg_l = neg_l.mean()
#   return neg_l


# divide continuous time scale into k discrete bins in total,  T_cont \in {[0, a_1), [a_1, a_2), ...., [a_(k-1), inf)}
# Y = T_discrete is the discrete event time:
# Y = -1 if T_cont \in (-inf, 0), Y = 0 if T_cont \in [0, a_1),  Y = 1 if T_cont in [a_1, a_2), ..., Y = k-1 if T_cont in [a_(k-1), inf)
# discrete hazards: discrete probability of h(t) = P(Y=t | Y>=t, X),  t = -1,0,1,2,...,k
# S: survival function: P(Y > t | X)
# all patients are alive from (-inf, 0) by definition, so P(Y=-1) = 0
# h(-1) = 0 ---> do not need to model
# S(-1) = P(Y > -1 | X) = 1 ----> do not need to model
'''
Summary: neural network is hazard probability function, h(t) for t = 0,1,2,...,k-1
corresponding Y = 0,1, ..., k-1. h(t) represents the probability that patient dies in [0, a_1), [a_1, a_2), ..., [a_(k-1), inf]
'''
def nll_loss(hazards, S, Y, c, alpha=0.4, eps=1e-7):
	batch_size = len(Y)
	Y = Y.view(batch_size, 1) # ground truth bin, 1,2,...,k
	c = c.view(batch_size, 1).float() #censorship status, 0 or 1
	if S is None:
		S = torch.cumprod(1 - hazards, dim=1) # surival is cumulative product of 1 - hazards
	# without padding, S(0) = S[0], h(0) = h[0]
	S_padded = torch.cat([torch.ones_like(c), S], 1) #S(-1) = 0, all patients are alive from (-inf, 0) by definition
	# after padding, S(0) = S[1], S(1) = S[2], etc, h(0) = h[0]
	#h[y] = h(1)
	#S[1] = S(1)
	uncensored_loss = -(1 - c) * (torch.log(torch.gather(S_padded, 1, Y).clamp(min=eps)) + torch.log(torch.gather(hazards, 1, Y).clamp(min=eps)))
	censored_loss = - c * torch.log(torch.gather(S_padded, 1, Y+1).clamp(min=eps))
	neg_l = censored_loss + uncensored_loss
	loss = (1-alpha) * neg_l + alpha * uncensored_loss
	loss = loss.mean()
	return loss

def ce_loss(hazards, S, Y, c, alpha=0.4, eps=1e-7):
	batch_size = len(Y)
	Y = Y.view(batch_size, 1) # ground truth bin, 1,2,...,k
	c = c.view(batch_size, 1).float() #censorship status, 0 or 1
	if S is None:
		S = torch.cumprod(1 - hazards, dim=1) # surival is cumulative product of 1 - hazards
	# without padding, S(0) = S[0], h(0) = h[0]
	# after padding, S(0) = S[1], S(1) = S[2], etc, h(0) = h[0]
	#h[y] = h(1)
	#S[1] = S(1)
	S_padded = torch.cat([torch.ones_like(c), S], 1)
	reg = -(1 - c) * (torch.log(torch.gather(S_padded, 1, Y)+eps) + torch.log(torch.gather(hazards, 1, Y).clamp(min=eps)))
	ce_l = - c * torch.log(torch.gather(S, 1, Y).clamp(min=eps)) - (1 - c) * torch.log(1 - torch.gather(S, 1, Y).clamp(min=eps))
	loss = (1-alpha) * ce_l + alpha * reg
	loss = loss.mean()
	return loss

# def nll_loss(hazards, Y, c, S=None, alpha=0.4, eps=1e-8):
#   batch_size = len(Y)
#   Y = Y.view(batch_size, 1) # ground truth bin, 1,2,...,k
#   c = c.view(batch_size, 1).float() #censorship status, 0 or 1
#   if S is None:
#       S = 1 - torch.cumsum(hazards, dim=1) # surival is cumulative product of 1 - hazards
#   uncensored_loss = -(1 - c) * (torch.log(torch.gather(hazards, 1, Y).clamp(min=eps)))
#   censored_loss = - c * torch.log(torch.gather(S, 1, Y).clamp(min=eps))
#   loss = censored_loss + uncensored_loss
#   loss = loss.mean()
#   return loss

class CrossEntropySurvLoss(object):
	def __init__(self, alpha=0.15):
		self.alpha = alpha

	def __call__(self, hazards, S, Y, c, alpha=None): 
		if alpha is None:
			return ce_loss(hazards, S, Y, c, alpha=self.alpha)
		else:
			return ce_loss(hazards, S, Y, c, alpha=alpha)

# loss_fn(hazards=hazards, S=S, Y=Y_hat, c=c, alpha=0)
class NLLSurvLoss(object):
	def __init__(self, alpha=0.15):
		self.alpha = alpha

	def __call__(self, hazards, S, Y, c, alpha=None):
		if alpha is None:
			return nll_loss(hazards, S, Y, c, alpha=self.alpha)
		else:
			return nll_loss(hazards, S, Y, c, alpha=alpha)
	# h_padded = torch.cat([torch.zeros_like(c), hazards], 1)
	#reg = - (1 - c) * (torch.log(torch.gather(hazards, 1, Y)) + torch.gather(torch.cumsum(torch.log(1-h_padded), dim=1), 1, Y))


class CoxSurvLoss(object):
	def __call__(hazards, S, c, **kwargs):
		# This calculation credit to Travers Ching https://github.com/traversc/cox-nnet
		# Cox-nnet: An artificial neural network method for prognosis prediction of high-throughput omics data
		current_batch_len = len(S)
		R_mat = np.zeros([current_batch_len, current_batch_len], dtype=int)
		for i in range(current_batch_len):
			for j in range(current_batch_len):
				R_mat[i,j] = S[j] >= S[i]

		R_mat = torch.FloatTensor(R_mat).to(device)
		theta = hazards.reshape(-1)
		exp_theta = torch.exp(theta)
		loss_cox = -torch.mean((theta - torch.log(torch.sum(exp_theta*R_mat, dim=1))) * (1-c))
		return loss_cox

def l1_reg_all(model, reg_type=None):
	l1_reg = None

	for W in model.parameters():
		if l1_reg is None:
			l1_reg = torch.abs(W).sum()
		else:
			l1_reg = l1_reg + torch.abs(W).sum() # torch.abs(W).sum() is equivalent to W.norm(1)
	return l1_reg

def l1_reg_modules(model, reg_type=None):
	l1_reg = 0

	l1_reg += l1_reg_all(model.fc_omic)
	l1_reg += l1_reg_all(model.mm)

	return l1_reg

def check_directories(args):
	r"""
	Updates the argparse.NameSpace with a custom experiment code.

	Args:
		- args (NameSpace)

	Returns:
		- args (NameSpace)
	"""
	
	feat_extractor = None
	if args.feats_dir:
		feat_extractor = args.feats_dir.split('/')[-1] if len(args.feats_dir.split('/')[-1]) > 0 else args.feats_dir.split('/')[-2]
		if feat_extractor == "RESNET50":
			args.path_input_dim = 2048 
		elif feat_extractor in ["PLIP", "CONCH"]:
			args.path_input_dim = 512 
		elif feat_extractor == "UNI":
			args.path_input_dim = 1024
		else:
			args.path_input_dim = 768

	args.split_dir = os.path.join(args.split_dir, args.data_name)
	print("split_dir", args.split_dir)
	assert os.path.isdir(args.split_dir)

	param_code = args.model_type.upper()
	if args.model_type in ["mcat", "motcat"]:
		args.mode = "coattn"	
	elif args.model_type == "deepattnmisl":
		args.mode = "cluster"	
	elif args.model_type in ["porpoise", "deepset", "amil"]:
		args.mode = "pathomic" 
	
	if feat_extractor:
		param_code += "_" + feat_extractor
	
	if args.omics not in ["None", "none", None]:
		args.run_name += "_"+args.omics if args.selected_features else "_"+args.omics+"_all"
		if args.apply_sig:
			args.run_name += "_sig"
	args.results_dir = os.path.join(args.results_dir, param_code, args.run_name)
	args.csv_path = f"{args.dataset_dir}/"+args.data_name+".csv" if not args.selected_features else f"{args.dataset_dir}/"+args.data_name+"_selected.csv"
	assert os.path.isfile(args.csv_path), f"Data file does not exist > {args.csv_path}"
	return args
