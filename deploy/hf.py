"""Deploy InvoiceFlow to a Hugging Face Space. Run from the project folder on your own computer:
    pip install -U huggingface_hub
    python deploy/hf.py
It asks for a write token and your Groq key (hidden), creates the Space, stores the key as a secret, and uploads the code."""
import getpass, sys
from huggingface_hub import HfApi

token = getpass.getpass("Hugging Face write token (hidden): ").strip()
api = HfApi(token=token)
user = api.whoami()["name"]
repo = f"{user}/{sys.argv[1] if len(sys.argv) > 1 else 'invoiceflow'}"

api.create_repo(repo, repo_type="space", space_sdk="docker", exist_ok=True, private=False)
key = getpass.getpass("Groq API key (hidden, Enter to skip): ").strip()
if key:
    api.add_space_secret(repo, "GROQ_API_KEY", key)
api.upload_folder(
    repo_id=repo, repo_type="space", folder_path=".",
    ignore_patterns=[".env", "data/*", "tests/*", "demo/*", ".git/*", "__pycache__/*", "*.pyc", ".pytest_cache/*", "app/mailer.py"],
    commit_message="Deploy InvoiceFlow",
)
print(f"\nBuilding. Watch it at https://huggingface.co/spaces/{repo}")
print(f"Live at https://{repo.replace('/', '-').lower()}.hf.space once it says Running")
