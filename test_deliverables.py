import requests
import json
import time

BASE_URL = "http://localhost:8000"

def test_health():
    print("\n--- Testing GET /health ---")
    try:
        response = requests.get(f"{BASE_URL}/health")
        print(f"Status: {response.status_code}")
        print(response.json())
    except Exception as e:
        print(f"Error: {e}")

def test_review_queue():
    print("\n--- Testing GET /review-queue ---")
    try:
        response = requests.get(f"{BASE_URL}/review-queue")
        print(f"Status: {response.status_code}")
        data = response.json()
        print(f"Total pending: {data.get('total_pending')}")
        if data.get('pairs'):
            print(f"First pair ID: {data['pairs'][0]['pair_id']}")
            return data['pairs'][0]
    except Exception as e:
        print(f"Error: {e}")
    return None

def test_decision_merge(pair):
    if not pair:
        print("\n--- Skipping POST /decision (no pair available) ---")
        return
    print("\n--- Testing POST /decision (MERGE) ---")
    payload = {
        "pair_id": pair['pair_id'],
        "left_record_id": pair['left']['source_record_id'],
        "right_record_id": pair['right']['source_record_id'],
        "left_source": pair['left']['source'],
        "right_source": pair['right']['source'],
        "decision": "MERGE",
        "analyst_id": "test_user",
        "notes": "Testing merge functionality"
    }
    try:
        response = requests.post(f"{BASE_URL}/decision", json=payload)
        print(f"Status: {response.status_code}")
        print(response.json())
        return response.json().get('ubid_assigned')
    except Exception as e:
        print(f"Error: {e}")
    return None

def test_refresh_attribution():
    print("\n--- Testing POST /query/refresh-attribution ---")
    try:
        response = requests.post(f"{BASE_URL}/query/refresh-attribution")
        print(f"Status: {response.status_code}")
        print(response.json())
    except Exception as e:
        print(f"Error: {e}")

def test_complex_query():
    print("\n--- Testing GET /query ---")
    try:
        # Test basic query
        response = requests.get(f"{BASE_URL}/query")
        print(f"Status (All): {response.status_code}")
        print(f"Found {len(response.json())} UBIDs")
        
        # Test filtered query
        response = requests.get(f"{BASE_URL}/query?status=ACTIVE")
        print(f"Status (Filtered): {response.status_code}")
    except Exception as e:
        print(f"Error: {e}")

def test_unmerge(pair_id):
    if not pair_id:
        print("\n--- Skipping POST /decision/unmerge (no pair_id) ---")
        return
    print("\n--- Testing POST /decision/unmerge ---")
    payload = {
        "pair_id": pair_id,
        "analyst_id": "test_user",
        "notes": "Testing unmerge functionality"
    }
    try:
        response = requests.post(f"{BASE_URL}/decision/unmerge", json=payload)
        print(f"Status: {response.status_code}")
        print(response.json())
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    print("Starting UBID Platform Backend Tests...")
    test_health()
    pair = test_review_queue()
    ubid = test_decision_merge(pair)
    test_refresh_attribution()
    test_complex_query()
    if pair:
        test_unmerge(pair['pair_id'])
    print("\nTests completed.")
