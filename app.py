from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['template_folder'] = 'templates'
app.config['static_folder'] = 'static'

db = SQLAlchemy(app)

class Asset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.String(80), unique=True, nullable=False)
    check_in = db.Column(db.DateTime, nullable=True)
    check_out = db.Column(db.DateTime, nullable=True)

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(
        db.String(80), 
        db.ForeignKey('asset.asset_id'), 
        nullable=False
    )
    timestamp = db.Column(db.DateTime, nullable=False)
    action = db.Column(db.String(50))

def create_tables():
    with app.app_context():
        db.create_all()

# Call the function to create database tables
create_tables()

def update_asset_status(asset_id, action):
    asset = Asset.query.filter_by(asset_id=asset_id).first()

    if not asset:
        asset = Asset(asset_id=asset_id)
        db.session.add(asset)

    if action == 'checkin':
        asset.check_in = datetime.now()
        asset.check_out = None
    elif action == 'checkout':
        asset.check_out = datetime.now()

    db.session.commit()

def add_event_to_history(asset_id, action):
    event = Event(asset_id=asset_id, timestamp=datetime.now(), action=action)
    db.session.add(event)
    db.session.commit()

@app.route('/api/assets', methods=['GET'])
def get_assets():
    try:
        assets = Asset.query.all()
        return jsonify([{'asset_id': asset.asset_id, 'check_in': asset.check_in, 'check_out': asset.check_out} for asset in assets])
    except Exception as e:
        print("Error:", e)
        return jsonify({'error': str(e)}), 500

@app.route('/asset_history')
def asset_history():
    asset_id = request.args.get('asset_id')
    if not asset_id:
        return jsonify({'error': 'Asset ID is required'}), 400

    try:
        events = Event.query.filter_by(asset_id=asset_id).all()
        asset_history = [{'timestamp': event.timestamp, 'action': event.action} for event in events]

        if not asset_history:
            return jsonify({'message': f'No history found for asset {asset_id}'}), 404

        return render_template('asset_history.html', asset_id=asset_id, asset_history=asset_history)
    except Exception as e:
        print(f"Error reading event history: {e}")
        return jsonify({'error': 'Internal Server Error'}), 500

@app.route('/api/assets/<string:asset_id>', methods=['POST'])
def update_asset(asset_id):
    try:
        action = request.json.get('action')
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
    return send_from_directory(app.config['static_folder'], filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8081)
    