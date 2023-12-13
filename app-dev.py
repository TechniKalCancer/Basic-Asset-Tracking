from flask import Flask, request, jsonify, render_template, send_from_directory
from csv import DictReader, DictWriter
from datetime import datetime
import json
import csv

app = Flask(__name__)
app.config['template_folder'] = 'templates'
app.config['static_folder'] = 'static'

# Path to static files
static_folder = 'static'

# Path to data files
assets_file = 'assets.csv'
events_file = 'events_history.csv'

def read_assets_csv(assets_file):
    assets = []
    with open(assets_file, 'r') as file:
        reader = csv.DictReader(file)
        for row in reader:
            assets.append(row)
    return assets

def update_asset_status(asset_id, action):
    # Read current assets data
    with open(assets_file, 'r') as f:
        reader = DictReader(f)
        assets = list(reader)

    # Find asset and update status
    for asset in assets:
        if asset['asset'] == asset_id:
            if action == 'checkin':
                asset['check_in'] = datetime.now().strftime('%m-%d-%Y %H:%M:%S')
                asset['check_out'] = None
            elif action == 'checkout':
                asset['check_out'] = datetime.now().strftime('%m-%d-%Y %H:%M:%S')
            break
    else:  # If asset not found, add new asset
        new_asset = {'asset': asset_id, 'check_in': None, 'check_out': None}
        if action == 'checkin':
            new_asset['check_in'] = datetime.now().strftime('%m-%d-%Y %H:%M:%S')
        elif action == 'checkout':
            new_asset['check_out'] = datetime.now().strftime('%m-%d-%Y %H:%M:%S')
        assets.append(new_asset)

    # Write updated data back to assets.csv
    with open(assets_file, 'w', newline='') as f:
        writer = DictWriter(f, fieldnames=['asset', 'check_in', 'check_out'])
        writer.writeheader()
        writer.writerows(assets)

def add_event_to_history(asset_id, action):
    timestamp = datetime.now().strftime('%m-%d-%Y %H:%M:%S')
    with open(events_file, 'a', newline='') as f:
        writer = DictWriter(f, fieldnames=['asset', 'timestamp', 'action'])
        if f.tell() == 0:  # Check if file is empty to write header
            writer.writeheader()
        writer.writerow({'asset': asset_id, 'timestamp': timestamp, 'action': action})

@app.route('/api/assets', methods=['GET'])
def get_assets():
    try:
        assets = []
        with open(assets_file, 'r') as file:
            reader = csv.DictReader(file)
            for row in reader:
                assets.append(row)
        return jsonify(assets)
    except Exception as e:
        # Log the exception for debugging
        print("Error:", e)
        return jsonify({'error': str(e)}), 500

@app.route('/asset_history', methods=['GET'])
def get_asset_history():
    asset_id = request.args.get('asset_id')
    if not asset_id:
        return jsonify({'error': 'Asset ID is required'}), 400

    try:
        with open(events_file, 'r') as f:
            reader = DictReader(f)
            asset_history = [row for row in reader if row['asset'] == asset_id]

        if not asset_history:
            return jsonify({'message': f'No history found for asset {asset_id}'}), 404

        return jsonify({'asset_id': asset_id, 'history': asset_history})

    except Exception as e:
        print(f"Error reading event history: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500

@app.route('/api/assets/<string:asset_id>', methods=['POST'])
def update_asset(asset_id):
    try:
        action = request.get_json()['action']
        update_asset_status(asset_id, action)
        add_event_to_history(asset_id, action)

        return jsonify({'message': f'Asset {action} successfully', 'asset': asset_id}), 200

    except Exception as e:
        print(f"Error updating asset: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(static_folder, filename)

if __name__ == '__main__':
    app.run(debug=True)
