import pandas as pd
import requests
import json

# 1. Load the candidate pairs you generated in Day 1
print("Loading candidate pairs...")
df = pd.read_csv("day1_candidate_pairs.csv")

# We just want to test the LLM on the first 5 pairs to ensure it works
test_pairs = df.head(5)

def generate_audit_summary(name_l, name_r, pin_code, match_prob):
    # Phi-3 is smart enough to handle direct, strict instructions without trickery
    prompt = f"""You are a strict, formal government audit AI. 
Analyze the following match:
- Record A: {name_l}
- Record B: {name_r}
- Shared Pin Code: {pin_code}
- Confidence Score: {match_prob * 100:.1f}%

Output EXACTLY one single sentence explaining why these records represent the same entity. Do not include introductory text, greetings, or follow-up instructions."""

    # Hit the local Ollama API
    response = requests.post("http://localhost:11434/api/generate", json={
        "model": "phi3", 
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.1  # Keep it highly focused and robotic
        }
    })
    
    # Grab the response and clean it up
    clean_summary = response.json()['response'].strip()
    
    # Safety net: grab only the first sentence/line if it gets chatty
    return clean_summary.split('\n')[0].replace('"', '').strip()


print("\n--- Generating Local LLM Summaries & Exporting ---")
final_results = []

for index, row in test_pairs.iterrows():
    summary = generate_audit_summary(
        row['business_name_l'], 
        row['business_name_r'], 
        row['pin_code_l'], 
        row['match_probability']
    )
    
    # Package the data into a clean dictionary
    match_data = {
        "id": int(index),
        "record_a_name": row['business_name_l'],
        "record_b_name": row['business_name_r'],
        "shared_pin": str(row['pin_code_l']),
        "confidence_score": round(row['match_probability'] * 100, 1),
        "ai_audit_summary": summary
    }
    final_results.append(match_data)
    print(f"✅ Processed Pair {index + 1}")

# Save it to a JSON file for the API
with open("final_review_queue.json", "w") as outfile:
    json.dump(final_results, outfile, indent=4)

print("\n🎉 Success! Data exported to final_review_queue.json")