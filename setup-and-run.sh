#!/bin/bash

# Setup
echo "📁 Creating virtual environment..."
python3 -m venv venv

echo "✅ Activating virtual environment..."
source venv/bin/activate

echo "📦 Installing dependencies..."
pip install -r requirements.txt

echo "🚀 Running geotagging script..."
python3 extract-coords.py
