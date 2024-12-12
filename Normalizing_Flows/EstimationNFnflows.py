#!/usr/bin/env python
# coding: utf-8

# Normalizing flow using nflows package and toy data 

import numpy as np
import matplotlib.pyplot as plt
import mplhep as hep
import scipy.stats
from scipy.stats import rel_breitwigner
import torch
import os
import argparse
import sklearn
#instead of manually defining bijectors and distributions, 
#import necessary components from nflows
from sklearn.preprocessing import StandardScaler, MinMaxScaler

from nflows.distributions.normal import StandardNormal
from nflows import distributions, flows, transforms
import nflows.transforms as transforms
from nflows.flows import Flow
from nflows.transforms.base import CompositeTransform
from nflows.transforms.autoregressive import MaskedPiecewiseRationalQuadraticAutoregressiveTransform
from nflows.transforms.permutations import ReversePermutation

from torch.utils.data import DataLoader, TensorDataset
import torch.optim as optim
from torch.optim.lr_scheduler import CosineAnnealingLR

# Use CMS style
hep.style.use("CMS")

# Add argument parsing for command line arguments
parser = argparse.ArgumentParser(description='Normalizing Flow Training Script')
parser.add_argument('--n_epochs', type=int, required=True, help='Number of epochs for training')
parser.add_argument('--learning_rate', type=float, required=True, help='Learning rate for training')
parser.add_argument('--batch_size', type=int, required=False, default=1024, help='Batch size for training')
parser.add_argument('--outdir', type=str, required=True, help='Output directory for saving results')
parser.add_argument('--num_layers', type=int, required=True, help='Number of flow layers (each flow layer contains several transformations/blocks)')
parser.add_argument('--num_blocks', type=int, required=True, help='Number of transformations/blocks in each flow layer')
parser.add_argument('--hidden_features', type=int, required=True, help='Number of neurons in the NN inside each transformation/block')
parser.add_argument('--num_bins', type=int, required=True, help='Number of network parameters for each layer of spline transformations')
args = parser.parse_args()

#Setup: 
# - bkg: exponential falling distribution
# - signal: Breit-Wigner at certain mass 

# Generate background and signal data
n_bkg = 800000
n_sig = 40
bkg = np.random.exponential(scale=100.0, size=n_bkg)
sig = rel_breitwigner.rvs(450, size=n_sig)
# Adding b-tagging information (a form of event classification)
bkg_btag = np.random.uniform(low=0.0, high=1.0, size=n_bkg)
sig_btag = np.random.normal(0.85, 0.05, n_sig)

num_features=2 #dimensionality of the data being transformed.
# In this case: b-tagging score and background energy

# Note: the bkg distribution is the posterior/target distribution which the Normalizing Flow should learn to approximate.

##Combining energy and b-tagging score for both bkg and signal 
bkg_coord = np.column_stack((bkg_btag, bkg))  # Combine btag and bkg for training
#Initialize the scaler 
scaler = StandardScaler()
#Scale the target distribution to help the model to converge faster 
bkg_coord_scaled = scaler.fit_transform(bkg_coord)

# Shift the entire dataset to make sure all values are positive
shift = -bkg_coord_scaled[:, 1].min() + 1e-6  # Get the absolute value of the minimum across all features

bkg_coord_scaled[:, 1] += shift  # Add the shift to the entire dataset

bkg_coord_scaled = bkg_coord_scaled.astype('float32') #bkg coordinates converted to float32 for compatibility with python 

# Define base distribution
base_distribution = distributions.StandardNormal(shape=(num_features,))

# Sample points from the base distribution
prior = base_distribution.sample(10000).numpy()  # Sample 10000 points with 2 features each


# Shift the prior distribution to ensure all values are positive
shift_prior = -prior[:, 1].min() + 1e-6  # Add a small constant to avoid zero values
prior[:, 1] += shift_prior  # Apply the shift to the energy feature



#Normalizing flow model:
# Set up simple normalizing flow with arbitrary inputs and outputs just to test 

# Define transformations (bijectors)
#transformations = transforms.MaskedAffineAutoregressiveTransform(features, args.hidden_features, args.num_blocks)

num_context=0

def make_flow(num_features,num_context, perm=True):
    base_dist = distributions.StandardNormal(shape=(num_features,))
    #base_dist = MultivariateScaledNormal(num_features, scale=3.0)

    transforms = []
    if num_context == 0:
        num_context = None
    for i in range(args.num_layers):
        transforms.append(MaskedPiecewiseRationalQuadraticAutoregressiveTransform(features=num_features,
                                                                                context_features=num_context,
                                                                                hidden_features=args.hidden_features,
                                                                                num_bins=args.num_bins,
                                                                                num_blocks=args.num_blocks,
                                                                                tail_bound=10.0, #range over which the spline trasnformation is defined 
                                                                                tails='linear',
                                                                                dropout_probability=0.2,
                                                                                use_batch_norm=False))
        if i < args.num_layers - 1 and perm:
            transforms.append(ReversePermutation(features=num_features)) #Shuffles feature order to increase expressivity
    transform = CompositeTransform(transforms)
    flow = Flow(transform, base_dist)
    return flow

# The higher the number of hidden_features/num_blocks, the more expressive the transformation will be, 
# allowing it to capture more complex relationships in the data.
# The neural network basically has the base distribution values as inputs and gets to the parameters of the target distribution (via the network). 
# Then those parameters are inserted in the target distribution to get the ouputs in correspondence to the inputs. In this case, the neural network has 16 layers. 
# Using a neural network inside the transformations in normalizing flows does make the training loop "deeper" 
# in the sense that you're not just applying a single transformation but a series of transformations that are learned through the neural network.

# Setting up the normalizing flow and the training loop

#Sample points from target distribution for training 
y = torch.from_numpy(bkg_coord_scaled[:100000])  # Take the first 100,000 samples

# Split the data into training and validation sets (e.g., 80% for training, 20% for validation)
train_size = int(0.8 * len(y))  # 80% for training
train_data = y[:train_size]
val_data = y[train_size:]

#Create and initialize the flow
#flow = Flow(transformations, base_distribution) #encapsules the entire flow model in a more structured way
flow = make_flow(num_features, num_context, perm=True)

#Training loop
opt = torch.optim.Adam(flow.parameters(), args.learning_rate)

train_dataset = TensorDataset(train_data)
val_dataset = TensorDataset(val_data)

#create loaders 
train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
val_loader = DataLoader(val_dataset, batch_size=args.batch_size)

# Define scheduler
scheduler = CosineAnnealingLR(optimizer=opt, T_max=args.n_epochs, eta_min=1e-3)  # eta_min: minimum LR, T_max: total number of epochs for one cosine cycle 
#scheduler = OneCycleLR(optimizer=opt, max_lr=5e-4, steps_per_epoch=len(train_loader), epochs=args.n_epochs)  

train_losses=[]
val_losses=[]

min_loss=np.inf # Initialize minimum validation loss
min_loss_epoch=-1
patience_counter=0 # Initialize patience counter

for idx in range(args.n_epochs):
    flow.train() #set to taining mode 
    total_train_loss=0.0
    
    #loop over training 
    for batch in train_loader:
        batch_data = batch[0]
        opt.zero_grad() #zero the gradients and make predictions for a set of inputs 
        
        # Minimize KL(p || q), i.e. calculate the loss
        train_loss = -flow.log_prob(batch_data).mean() #calculating the log probability for batch
        train_loss.backward() 
        opt.step() #model updates its parameters (weights) 
        total_train_loss += train_loss.item() * batch_data.size(0) #accumulate batch loss 

    #Calculate average training loss for the current epoch
    avg_train_loss = total_train_loss / len(train_data)
    # Append the loss for plotting later
    train_losses.append(avg_train_loss)
    
    #Validation loss (analogously to training loss)
    flow.eval() #set to evaluation mode 
    total_val_loss=0.0
    
    #loop over validation 
    with torch.no_grad():
        for val_batch in val_loader:
            val_batch_data = val_batch[0]
            
            # Minimize KL(p || q)
            val_loss = -flow.log_prob(val_batch_data).mean() #calculating the log probability for validation
            total_val_loss += val_loss.item() * val_batch_data.size(0) #accumulate batch loss 
    
    #Calculate average validation loss for the current epoch
    avg_val_loss = total_val_loss / len(val_data) 
    # Append the loss for plotting later
    val_losses.append(avg_val_loss)
    
    # Scheduler step for CosineAnnealingLR
    scheduler.step() #adjusts the learning rate at each epoch based on cosine formula 

    # Early stopping based on patience mechanism 
    # Patience mechanism keeps track of how many epochs have passed without improvement in loss. 
    # If the loss does not improve for 5 consecutive epochs, the training stops early to prevent overfitting.

    if avg_val_loss < min_loss: #compare the epoch-level validation loss to min_loss
        min_loss = avg_val_loss
        min_loss_epoch = idx # Track the epoch where minimum loss occurs
        patience_counter = 0 # Reset patience counter if the loss improves
        
        # Save the best model
        model_path = os.path.join(args.outdir, "best_model.pth")
        torch.save(flow.state_dict(), model_path)  
        # flow.state_dict() returns a dictionary containing all the parameters of the model (weights and biases)
        # torch.save() saves the parameters to a file named best_model.pth
    else:
        patience_counter += 1
        if patience_counter >= 10: # If no improvement for 5 epochs, stop training
            print("Early stopping triggered")
            break
    
    # Use val_loss for early stopping because it provides a measure of how well the model generalizes to unseen data.
    # By monitoring val_loss instead of train_loss, we ensure that training does not continue if the model starts to overfit.
    # Early stopping based on val_loss helps prevent the model from improving solely on training data performance, which could lead to overfitting.   

    # Print progress every epoch
    if idx % 1 == 0:
        print(f"Epoch {idx}, Avg Train Loss: {avg_train_loss:.4f}, Avg Validation Loss: {avg_val_loss:.4f}")

# Load the best model after training
model_path = os.path.join(args.outdir, "best_model.pth")
flow.load_state_dict(torch.load(model_path, weights_only=True))
flow.eval()  # Set the model to evaluation mode
print("Best model loaded successfully.")
    
# Sample points from the trained flow
trained = flow.sample(10000).detach().numpy()  # Sample 10000 points with 2 features each

# Sample points from the base distribution
prior = base_distribution.sample(10000).numpy()  # Sample 10000 points with 2 features each

# Function to calculate KL divergence between target and trained distribution
def calculate_kl_divergence(target, trained, eps=1e-8):
    # Ensure target and trained are in probability space and avoid log(0) errors
    target = np.clip(target, eps, None)  # Clip target values to avoid zero probabilities
    trained = np.clip(trained, eps, None)  # Same for trained values
    # This prevents taking the logarithm of zero, which would lead to undefined (NaN) values and numerical instability in the KL divergence calculation. 
    # By clipping to this small positive value, it ensures no probability is exactly zero.
    
    # Normalize to make them proper probability distributions
    target /= np.sum(target)
    trained /= np.sum(trained)

    # Convert numpy arrays to PyTorch tensors
    p_target = torch.from_numpy(target).float()
    q_trained = torch.from_numpy(trained).float().log()  # q_trained should be in log space

    # Calculate KL divergence
    kl_divergence = torch.nn.functional.kl_div(q_trained, p_target, reduction='batchmean')
    return kl_divergence.item()

# Calculate KL divergence
kl_div = calculate_kl_divergence(bkg_coord_scaled[:10000], trained)
print("KL divergence saved successfully.")
# Save KL divergence value
kl_div_path = os.path.join(args.outdir, "kl_divergence.npy")
np.save(kl_div_path, kl_div)
print("KL divergence saved successfully.")

# Create output directory if it doesn't exist
os.makedirs(args.outdir, exist_ok=True)

# After creating the scatter plot
plt.scatter(prior[:, 0], prior[:, 1], color='gray', label='Base/Prior distribution')
plt.scatter(bkg_coord_scaled[:10000, 0], bkg_coord_scaled[:10000, 1], color='blue', label='Background/Target distribution')
plt.scatter(trained[:, 0], trained[:, 1], color='green', label='Trained distribution')
plt.xlabel("Latent b-tagging score")
plt.ylabel("Scaled Energy")
plt.legend(loc='upper right',fontsize=16)

# Display hidden_features, num_blocks, and KL divergence in the plot
text_str = f"learning_rate: {args.learning_rate}\nnum_layers: {args.num_layers}\nnum_blocks: {args.num_blocks}\nhidden_features: {args.hidden_features}\nnum_bins: {args.num_bins}\nn_epochs: {args.n_epochs}"
plt.text(0.05, 0.95, text_str, transform=plt.gca().transAxes, fontsize=16, verticalalignment='top',
         bbox=dict(boxstyle="round,pad=0.3", edgecolor='black', facecolor='white', alpha=0.7))

# Save the plot to the output directory
scatter_name = f"scatter.png"
scatter_path = os.path.join(args.outdir, scatter_name)
plt.savefig(scatter_path)

# Plot training and validation loss per epoch
# Training loss: measures how well your model is fitting the target distribution
# Validation loss: measures how well your model generalizes to unseen data fro the same target disribution

plt.figure()
plt.plot(train_losses, label="Training Loss", color='blue') #training loss 
plt.plot(val_losses, label="Validation Loss", color='red', linestyle='--') #validation loss 
plt.xlabel("Epochs")
plt.ylabel("Loss")
plt.legend(fontsize=20)

# Display the minimum loss and the corresponding epoch in the plot
text_str = f"Min Loss: {min_loss:.4f} at Epoch {min_loss_epoch}\nKL Divergence: {kl_div:.9f}"
plt.text(0.6, 0.95, text_str, transform=plt.gca().transAxes, fontsize=16, verticalalignment='top',
         bbox=dict(boxstyle="round,pad=0.3", edgecolor='black', facecolor='white', alpha=0.7))

# Save the training loss plot
loss_name = f"loss.png"
loss_plot_path = os.path.join(args.outdir, loss_name)
plt.savefig(loss_plot_path)

# Function to plot marginal distributions
def plot_marginals(target, trained, feature_names, bins, outdir, scaler):
    num_features = target.shape[1]
    
    for i in range(num_features):
        fig, (ax_main, ax_ratio) = plt.subplots(2, 1, figsize=(8, 10), gridspec_kw={'height_ratios': [3, 1]})
        
        # Initialize target_feature and trained_feature variables
        target_feature = None
        trained_feature = None
        x_limits = None
        
        # Use log of the energy for plotting if the feature is energy (index 1)
        if i == 1:  # Assuming energy is the second feature (index 1)
            # Inverse transform the target and trained data to get the original values
            target_feature_original = scaler.inverse_transform(np.column_stack((target[:, 0], target[:, 1])))[:, 1]
            trained_feature_original = scaler.inverse_transform(np.column_stack((trained[:, 0], trained[:, 1])))[:, 1]
            
            # Add a small constant to avoid log(0) or negative values
            valid_target = target_feature_original > 0
            valid_trained = trained_feature_original > 0
            
            # Plot original energy (non-log transformed)
            target_feature = target_feature_original[valid_target]
            trained_feature = trained_feature_original[valid_trained]
            feature_label = f"{feature_names[i]}"
            
            # Set x-axis limits for energy feature
            x_limits = (-10.5, 2.5)
            
        else:
            target_feature_original = scaler.inverse_transform(np.column_stack((target[:, 0], target[:, 1])))[:, i]
            trained_feature_original = scaler.inverse_transform(np.column_stack((trained[:, 0], trained[:, 1])))[:, i]
            
            target_feature = target_feature_original
            trained_feature = trained_feature_original
            feature_label = feature_names[i]
            
            # Set x-axis limits for non-energy features (e.g., b-tagging score)
            x_limits = (-2.5, 2.5)
        
        # Determine global range for bin edges
        all_data = np.concatenate([target_feature, trained_feature])
        global_min, global_max = np.min(all_data), np.max(all_data)
        bin_edges = np.linspace(global_min, global_max, bins + 1)  # Define bins globally
        
        # Main plot (marginal distribution)
        hist_target, bins_target = np.histogram(target_feature, bins=bin_edges, density=True)
        hist_trained, bins_trained = np.histogram(trained_feature, bins=bin_edges, density=True)

        # Error Propagation: calculate errors for the histograms (square root of the counts for each bin)
        err_target = np.sqrt(hist_target)
        err_trained = np.sqrt(hist_trained)
        
        # Plot histograms with error bars
        ax_main.errorbar(bins_target[:-1] + np.diff(bins_target) / 2, hist_target, yerr=err_target, fmt='o', alpha=0.5, label='Target', color='blue')
        ax_main.errorbar(bins_trained[:-1] + np.diff(bins_trained) / 2, hist_trained, yerr=err_trained, fmt='o', alpha=0.5, label='Trained', color='green')
        ax_main.set_xlabel(feature_label, fontsize=20)
        ax_main.set_ylabel("Density", fontsize=20)
        ax_main.legend(fontsize=16)
        ax_main.set_xlim(x_limits)  # Apply feature-specific x-axis limits

        '''
        ax_main.hist(target_feature, bins=bin_edges, alpha=0.5, label='Target', density=True, color='blue')
        ax_main.hist(trained_feature, bins=bin_edges, alpha=0.5, label='Trained', density=True, color='green')
        ax_main.set_xlabel(feature_label, fontsize=20)
        ax_main.set_ylabel("Density", fontsize=20)
        ax_main.legend(fontsize=16)
        ax_main.set_xlim(x_limits)  # Apply feature-specific x-axis limits
        '''

        # Calculate bin-by-bin ratio of counts (target/trained)
        
        # Remove empty bins (bins where count is zero)
        non_zero_bins_target = hist_target > 0
        non_zero_bins_trained = hist_trained > 0

        # Filter the bins and counts to exclude empty bins
        bins_target_filtered = bins_target[:-1][non_zero_bins_target]  # Exclude last bin edge
        hist_target_filtered = hist_target[non_zero_bins_target]
        
        bins_trained_filtered = bins_trained[:-1][non_zero_bins_trained]  # Exclude last bin edge
        hist_trained_filtered = hist_trained[non_zero_bins_trained]
        
        # Align bins and calculate ratio (ensure bins match by filtering similarly)
        common_bins = np.intersect1d(bins_target_filtered, bins_trained_filtered)  # Find common bins
        bin_centers = (common_bins[:-1] + common_bins[1:]) / 2  # Bin centers

        # Recalculate the histograms using only common bins
        target_hist_common = np.interp(common_bins[:-1], bins_target_filtered, hist_target_filtered)
        trained_hist_common = np.interp(common_bins[:-1], bins_trained_filtered, hist_trained_filtered)
        
        # Calculate errors for the ratio (error propagation)
        err_target_common = np.interp(common_bins[:-1], bins_target_filtered, np.sqrt(hist_target_filtered))
        err_trained_common = np.interp(common_bins[:-1], bins_trained_filtered, np.sqrt(hist_trained_filtered))
        
        # Ratio and its error
        ratio = trained_hist_common / (target_hist_common + 1e-6)  # Avoid division by zero
        err_ratio = ratio * np.sqrt((err_target_common / target_hist_common)**2 + (err_trained_common / trained_hist_common)**2)
        
        # Ratio plot (target/trained ratio)
        ax_ratio.errorbar(bin_centers, ratio, yerr=err_ratio, fmt='o', label='Trained/Target Ratio', color='red', alpha=0.7)
        ax_ratio.set_ylabel("Ratio (Trained/Target)", fontsize=16)
        ax_ratio.legend(fontsize=14)
        ax_ratio.set_xlim(x_limits)  # Apply feature-specific x-axis limits
        ax_ratio.axhline(y=1, color='black', linestyle='--', linewidth=2)  # Horizontal line at y=1
        
        '''
        # Ratio plot (target/trained ratio)
        ax_ratio.plot(bin_centers, ratio, label='Trained/Target Ratio', color='red', alpha=0.7)
        ax_ratio.set_ylabel("Ratio (Trained/Target)", fontsize=16)
        ax_ratio.legend(fontsize=14)
        ax_ratio.set_xlim(x_limits)  # Apply feature-specific x-axis limits
        ax_ratio.axhline(y=1, color='black', linestyle='--', linewidth=2)  # Horizontal line at y=1
        '''

        # Save the plot
        plot_path = os.path.join(outdir, f"marginal_feature_{i+1}.png")
        plt.tight_layout()
        plt.savefig(plot_path)
        plt.close()

# Call the function with the necessary arguments
feature_names = ["b-tagging score", "energy scaled"]
n_bins=35
plot_marginals(bkg_coord_scaled[:10000], trained, feature_names, n_bins, args.outdir, scaler)
        