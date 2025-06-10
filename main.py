import os
from flask import jsonify
from google.cloud import firestore
import requests
import functions_framework

# Constants for external endpoints
SIERRA_WEBHOOK_EP = "https://api.sierrainteractivedev.com/webhook"
SIERRA_USERS_EP = "https://api.sierrainteractivedev.com/users?name="


def get_firestore_client():
    """Create and return a Firestore client."""
    return firestore.Client()

FIRESTORE_COLLECTION = os.getenv('FIRESTORE_COLLECTION')
CF_HANDLER_URL = os.getenv('CF_HANDLER_URL')

if not FIRESTORE_COLLECTION or not CF_HANDLER_URL:
    raise RuntimeError('FIRESTORE_COLLECTION and CF_HANDLER_URL environment variables must be set.')

@functions_framework.http
def main(request):
    """
    HTTP Cloud Function to read Firestore collection and manage Sierra subscriptions.
    Only GET requests are allowed.
    """
    if request.method != 'GET':
        return jsonify({'error': 'Method not allowed'}), 405
    db = get_firestore_client()
    try:
        docs = db.collection(FIRESTORE_COLLECTION).stream()
    except Exception as e:
        return jsonify({"error": f"Failed to read Firestore collection: {str(e)}"}), 500
    results = []
    successes = []
    failures = []
    skipped = []
    for doc in docs:
        data = doc.to_dict()
        data['id'] = doc.id
        results.append(data)

    for result in results:
        should_make_new_subscription = False
        site_name = result.get('id', '')
        client_name = result.get('Client', None)
        subscription_id = result.get('id')
        users = result.get('availableUsers', {})
        allowed_user_ids = result.get('allowedAdminUserIds', [])
        if not users:
            failures.append({"site_name": site_name, "Client": client_name, "reason": "Missing availableUsers in Firestore document"})
            continue
        user_names = [name for name in users.values() if isinstance(name, str)]
        if not user_names:
            failures.append({"site_name": site_name, "Client": client_name, "reason": "No valid user names found in availableUsers"})
            continue
        if not subscription_id:
            skipped.append({"site_name": site_name, "Client": client_name, "reason": "skipped due to missing subscription ID"})
            continue
        api_key = result.get('apiKey')
        if not api_key:
            failures.append({"site_name": site_name, "Client": client_name, "reason": "Missing apiKey in Firestore document"})
            continue
        # Prepare webhook subscription body and headers
        body = {
            "eventTypes": ["LeadCommunicationLogged"],
            "url": f"{CF_HANDLER_URL}?site_name={site_name}",
            "exceptSystemName": "Automations"
        }
        headers = {
            "Content-Type": "application/json",
            "Sierra-ApiKey": api_key,
            "Sierra-OriginatingSystemName": "Automations Webhook",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        # Check existing Sierra subscriptions
        try:
            response = requests.get(SIERRA_WEBHOOK_EP, headers=headers)
            if response.status_code != 200:
                failures.append({"site_name": site_name, "Client": client_name, "reason": f"Failed to fetch data from Sierra endpoint", "status_code": response.status_code})
                continue
            response_data = response.json()
        except Exception as e:
            failures.append({"site_name": site_name, "Client": client_name, "reason": f"Sierra API request failed: {str(e)}"})
            continue
        subscriptions = response_data.get('data', [])
        for subscription in subscriptions:
            if subscription.get('id') == result.get('subscriptionID') and subscription.get('banned', False):
                should_make_new_subscription = True
                break
        # Create new subscription if needed
        if should_make_new_subscription:
            try:
                response = requests.post(SIERRA_WEBHOOK_EP, json=body, headers=headers)
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
        # Update allowedAdminUserIds for users
        for user_name in user_names:
            try:
                user_response = requests.get(f"{SIERRA_USERS_EP}{user_name}", headers=headers)
                if user_response.status_code != 200:
                    failures.append({"site_name": site_name, "Client": client_name, "reason": f"Failed to fetch user {user_name} from Sierra", "status_code": user_response.status_code})
                    continue
                user_data = user_response.json()['data']
                if not user_data:
                    failures.append({"site_name": site_name, "Client": client_name, "reason": f"User {user_name} not found in Sierra"})
                    continue
                users_in_sierra = user_data.get('records', [])
                for user_record in users_in_sierra:
                    user_id = user_record.get('id')
                    record_name = user_record.get('name')
                    if user_id and user_id not in allowed_user_ids and record_name == user_name:
                        db.collection(FIRESTORE_COLLECTION).document(result['id']).update({
                            'allowedAdminUserIds': firestore.ArrayUnion([user_id]),
                            f"availableUsers.{user_id}": user_name
                        })
                        # Not adding to successes here, only for subscriptionID update
            except Exception as e:
                failures.append({"site_name": site_name, "Client": client_name, "reason": f"User API request failed: {str(e)}"})
                continue

    return jsonify({
        "message": "Webhook processing complete",
        "successes": successes,
        "failures": failures,
        "skipped": skipped
    }), 200