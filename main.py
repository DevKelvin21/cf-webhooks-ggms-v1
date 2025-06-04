import os
from flask import jsonify
from google.cloud import firestore
import requests
import functions_framework

def get_firestore_client():
    """Create and return a Firestore client."""
    return firestore.Client()

FIRESTORE_COLLECTION = os.getenv('FIRESTORE_COLLECTION')
CF_HANDLER_URL = os.getenv('CF_HANDLER_URL')

if not FIRESTORE_COLLECTION or not CF_HANDLER_URL:
    raise RuntimeError('FIRESTORE_COLLECTION and CF_HANDLER_URL environment variables must be set.')

@functions_framework.http
def main(request):
    """HTTP Cloud Function to read Firestore collection and manage Sierra subscriptions."""
    if request.method != 'GET':
        return (jsonify({'error': 'Method not allowed'}), 405)
    db = get_firestore_client()
    try:
        docs = db.collection(FIRESTORE_COLLECTION).stream()
    except Exception as e:
        return (jsonify({"error": f"Failed to read Firestore collection: {str(e)}"}), 500)
    results = []
    successes = []
    failures = []
    skipped = []
    for doc in docs:
        data = doc.to_dict()
        data['id'] = doc.id
        results.append(data)

    sierra_ep = "https://api.sierrainteractivedev.com/webhook"
    for result in results:
        should_make_new_subscription = False
        site_name = result.get('id', '')
        client_name = result.get('Client', None)
        subscription_id = result.get('id')
        if not subscription_id:
            skipped.append({"site_name": site_name, "Client": client_name, "reason": "skipped due to missing subscription ID"})
            continue
        body = {
            "eventTypes": ["LeadCommunicationLogged"],
            "url": f"{CF_HANDLER_URL}?site_name={site_name}",
            "exceptSystemName": "Automations"
        }
        api_key = result.get('apiKey')
        if not api_key:
            failures.append({"site_name": site_name, "Client": client_name, "reason": "Missing apiKey in Firestore document"})
            continue
        headers = {
            "Content-Type": "application/json",
            "Sierra-ApiKey": api_key,
            "Sierra-OriginatingSystemName": "Automations Webhook",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        try:
            response = requests.get(sierra_ep, headers=headers)
            if response.status_code != 200:
                failures.append({"site_name": site_name, "Client": client_name, "reason": f"Failed to fetch data from Sierra endpoint", "status_code": response.status_code})
                continue
            response_data = response.json()
        except Exception as e:
            failures.append({"site_name": site_name, "Client": client_name, "reason": f"Sierra API request failed: {str(e)}"})
            continue
        subscriptions = response_data.get('data', [])
        for subscription in subscriptions:
            if subscription.get('id') == subscription_id and subscription.get('banned', False):
                should_make_new_subscription = True
                break
        if should_make_new_subscription:
            try:
                response = requests.post(sierra_ep, json=body, headers=headers)
                if response.status_code != 200:
                    failures.append({"site_name": site_name, "Client": client_name, "reason": f"Failed to create new subscription", "status_code": response.status_code})
                    continue
                result['subscriptionID'] = response.json().get('id')
                db.collection(FIRESTORE_COLLECTION).document(result['id']).update({
                    'subscriptionID': result['subscriptionID'],
                    'new_subscription': True
                })
                successes.append({"site_name": site_name, "Client": client_name})
            except Exception as e:
                failures.append({"site_name": site_name, "Client": client_name, "reason": f"Failed to create/update subscription: {str(e)}"})
                continue

    return (jsonify({
        "message": "Webhook processing complete",
        "successes": successes,
        "failures": failures,
        "skipped": skipped
    }), 200)