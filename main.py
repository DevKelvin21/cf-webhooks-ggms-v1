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
    for doc in docs:
        data = doc.to_dict()
        data['id'] = doc.id
        results.append(data)

    sierra_ep = "https://api.sierrainteractivedev.com/webhook"
    for result in results:
        should_make_new_subscription = False
        site_name = result.get('id', '')
        body = {
            "eventTypes": ["LeadCommunicationLogged"],
            "url": f"{CF_HANDLER_URL}?site_name={site_name}",
            "exceptSystemName": "Automations"
        }
        api_key = result.get('apiKey')
        if not api_key:
            return (jsonify({"error": f"Missing apiKey in Firestore document {site_name}"}), 400)
        headers = {
            "Content-Type": "application/json",
            "Sierra-ApiKey": api_key,
            "Sierra-OriginatingSystemName": "Automations Webhook",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        try:
            response = requests.get(sierra_ep, headers=headers)
            if response.status_code != 200:
                return (jsonify({
                    "error": "Failed to fetch data from Sierra endpoint",
                    "status_code": response.status_code
                }), response.status_code)
            response_data = response.json()
        except Exception as e:
            return (jsonify({"error": f"Sierra API request failed: {str(e)}"}), 500)
        subscriptions = response_data.get('data', [])
        subscription_id = result.get('id')
        if subscription_id:
            for subscription in subscriptions:
                if subscription.get('id') == subscription_id and subscription.get('banned', False):
                    should_make_new_subscription = True
                    break
        if should_make_new_subscription:
            try:
                response = requests.post(sierra_ep, json=body, headers=headers)
                if response.status_code != 200:
                    return (jsonify({
                        "error": "Failed to create new subscription",
                        "status_code": response.status_code
                    }), response.status_code)
                result['new_subscription'] = True
                result['subscriptionID'] = response.json().get('id')
                db.collection(FIRESTORE_COLLECTION).document(result['id']).update({
                    'subscriptionID': result['subscriptionID']
                })
            except Exception as e:
                return (jsonify({"error": f"Failed to create/update subscription: {str(e)}"}), 500)

    return (jsonify({
        "message": "Data retrieved successfully",
        "data": results,
        "sierra_endpoint": sierra_ep
    }), 200)