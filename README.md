# Asset Tracking System

This is a simple asset tracking system built with Flask, SQLAlchemy, and JavaScript. It allows you to check in and check out assets, and view the history of asset actions.

## Prerequisites

- Docker
- Docker Compose

## Setup

1. **Clone the repository:**

    ```sh
    git clone https://github.com/yourusername/asset-tracking-system.git
    cd asset-tracking-system
    ```

2. **Create a `.env` file:**

    Create a `.env` file in the root directory of the project and add your environment variables. For example:

    ```env
    DATABASE_URL=sqlite:///assets.db
    ```

3. **Build and run the Docker containers:**

    ```sh
    docker-compose up --build
    ```

4. **Access the application:**

    Open your web browser and go to `http://localhost:8081`.

## Project Structure

- `app.py`: The main Flask application file.
- `models.py`: Contains the SQLAlchemy models for the database.
- `static/`: Contains static files like CSS, JavaScript, and images.
- `templates/`: Contains HTML templates for rendering the web pages.
- `Dockerfile`: Docker configuration file for building the Docker image.
- `docker-compose.yml`: Docker Compose configuration file for setting up the services.
- `.env`: Environment variables file (not included in the repository).
- `.dockerignore`: Specifies which files and directories to ignore when building the Docker image.
- `.gitignore`: Specifies which files and directories to ignore in the Git repository.

## API Endpoints

- `GET /api/assets`: Retrieve all assets.
- `POST /api/assets/<asset_id>`: Check in or check out an asset.
- `GET /asset_history`: Retrieve the history of actions for a specific asset.
