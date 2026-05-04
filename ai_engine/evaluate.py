import pandas as pd

# 1. Load the data
print("Loading predictions and ground truth...")
preds = pd.read_csv("day1_candidate_pairs.csv")
truth = pd.read_csv("synthetic_data/ground_truth.csv")

# 2. Create a lookup dictionary: source_record_id -> real_business_idx
# This lets us check if two predicted records actually belong to the same business
truth_map = pd.Series(truth.real_business_idx.values, index=truth.source_record_id).to_dict()

# 3. Evaluate Predictions (Precision)
true_positives = 0
false_positives = 0

for _, row in preds.iterrows():
    # Splink outputs the IDs as unique_id_l and unique_id_r
    id_l = row['unique_id_l']
    id_r = row['unique_id_r']
    
    # Look up the true business index for both
    real_idx_l = truth_map.get(id_l)
    real_idx_r = truth_map.get(id_r)
    
    # If they share the same real_business_idx, it's a correct match!
    if real_idx_l and real_idx_r and (real_idx_l == real_idx_r):
        true_positives += 1
    else:
        false_positives += 1

# 4. Calculate Maximum Possible Matches (Recall Baseline)
# How many true pairs actually exist in the ground truth?
real_groups = truth.groupby('real_business_idx')['source_record_id'].apply(list)
total_actual_pairs = 0
for records in real_groups:
    n = len(records)
    if n > 1:
        # Combinations formula: n * (n-1) / 2
        total_actual_pairs += (n * (n - 1)) // 2

false_negatives = total_actual_pairs - true_positives

# 5. The Final Scorecard
precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) > 0 else 0
recall = true_positives / total_actual_pairs if total_actual_pairs > 0 else 0

print("\n--- 📊 ENGINE SCORECARD ---")
print(f"Total Predicted Pairs: {len(preds)}")
print(f" True Positives (Correct Matches): {true_positives}")
print(f" False Positives (Wrong Merges): {false_positives}")
print(f"" False Negatives (Missed Matches): {false_negatives}")
print("---------------------------")
print(f" Precision: {precision * 100:.1f}% (When it guesses match, how often is it right?)")
print(f" Recall:    {recall * 100:.1f}% (Out of all true matches, how many did it find?)")