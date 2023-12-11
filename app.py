from flask import Flask, request, jsonify, render_template, send_from_directory
from csv import DictReader, DictWriter
from ast import literal_eval  # Import literal_eval to safely evaluate the string as a Python expression
from datetime import datetime

app = Flask(__name__)
app.config['template_folder'] = 'templates'
app.config['static_folder'] = 'static'

# Path to static files
static_folder = 'static'

# Path to data file
data_file = 'assets.csv'

def update_csv(data):
    with open(data_file, 'w') as f:
        writer = DictWriter(f, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)

def get_asset_by_id(data, asset_id):
    for item in data:
        if item['asset'] == asset_id:
            return item
    return None

def add_event(asset, action):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S %f')[:-6]
    event = {'timestamp': timestamp, 'action': action}

    # Check if the asset already has a 'history' field and it's a string
    if 'history' in asset and isinstance(asset['history'], str):
        # Convert the string representation to a list of dictionaries
        asset['history'] = literal_eval(asset['history'])

    # Check if the asset already has a 'history' field
    if 'history' in asset and isinstance(asset['history'], list):
        asset['history'].append(event)
    else:
        asset['history'] = [event]


def add_new_asset(data, asset_id):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S %f')[:-6]
    new_asset = {'asset': asset_id, 'history': [{'timestamp': timestamp, 'action': 'checkin'}]}
    data.append(new_asset)
    update_csv(data)
    return new_asset

@app.route('/api/assets', methods=['GET'])
def get_assets():
    with open(data_file, 'r') as f:
        reader = DictReader(f)
        data = list(reader)

        # Convert the string representation to a list of dictionaries
        for item in data:
            item['history'] = literal_eval(item['history'])

    return jsonify(data)

@app.route('/api/assets/<string:asset_id>', methods=['POST'])
def update_asset(asset_id):
    try:
        with open(data_file, 'r') as f:
            reader = DictReader(f)
            data = list(reader)

        action = request.get_json()['action']

        existing_asset = get_asset_by_id(data, asset_id)

        if existing_asset:
            add_event(existing_asset, action)
        else:
            new_asset = add_new_asset(data, asset_id)
            existing_asset = new_asset

        update_csv(data)

        return jsonify({'message': 'Asset updated successfully', 'asset': existing_asset}), 200

    except Exception as e:
        print(f"Error updating asset: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500

# Add route for the root path
@app.route('/')
def index():
    return render_template('index.html')

# Serve static files
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(static_folder, filename)

if __name__ == '__main__':
    # Use waitress to serve the app
    app.run(debug=True)
    
