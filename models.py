from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Asset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.String(80), unique=True, nullable=False)
    check_in = db.Column(db.DateTime, nullable=True)
    check_out = db.Column(db.DateTime, nullable=True)

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.String(80), db.ForeignKey('asset.asset_id'), nullable=False)
    timestamp = db.Column(db.DateTime, nullable=False)
    action = db.Column(db.String(50))
