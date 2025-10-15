Option 1: Using Docker (Recommended)
This is the most straightforward method as it automatically handles all dependencies and configuration.

  1. Build and run the application with Docker Compose:
    Open your terminal in the project root and run:


  1     docker-compose up

    This will build the Docker image and start the application. You can access it at http://localhost:8000.

Option 2: Running Locally with a Virtual Environment

This method requires you to set up a Python environment on your machine. This project uses uv, a fast Python package installer.


  1. Install `uv`:
    Since you are on macOS, you can install uv by running this command in your terminal:


  1     curl -LsSf https://astral.sh/uv/install.sh | sh


  2. Create a virtual environment:
    Once uv is installed, create a virtual environment:

  1     uv venv

    This will create a .venv directory in your project folder.


  3. Activate the virtual environment:

  1     source .venv/bin/activate


  4. Install dependencies:

  1     uv sync


  5. Run the application:


  1     uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

    The application will be available at http://localhost:8000.


Note: For the local setup, make sure you have a Python version compatible with the project's requirement (>=3.13).