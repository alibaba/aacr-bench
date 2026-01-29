import subprocess
from pathlib import Path
import os
import requests


def _get_workspace_and_origin(workspace: str):
    """
    Get the workspace path and original directory
    """
    workspace_path = Path(workspace)
    if not workspace_path.exists():
        raise ValueError(f"Workspace directory does not exist: {workspace}")

    if not workspace_path.is_dir():
        raise ValueError(f"Workspace path is not a directory: {workspace}")

    # Check if it's a git repository
    git_dir = os.path.join(workspace, '.git')
    if not os.path.exists(git_dir):
        raise ValueError(f"Not a git repository: {workspace}")

    # Change to the workspace directory
    original_dir = os.getcwd()
    return workspace_path, original_dir

def git_clone(git_repo_url: str):
    """
    Clone a git repository to the specified directory
    """
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

    # Create the absolute path for the repo directory
    repo_dir = "repos"
    abs_repo_dir = os.path.join(project_root, repo_dir)
    os.makedirs(abs_repo_dir, exist_ok=True)

    # Extract repo name from URL for the target directory
    repo_name = git_repo_url.rstrip('/').split('/')[-1].replace('.git', '')
    target_dir = os.path.join(abs_repo_dir, repo_name)

    # Clone the repository, if target dir exists, return
    if os.path.exists(target_dir):
        return target_dir
    try:
        result = subprocess.run(
            ['git', 'clone', git_repo_url, str(target_dir)],
            check=True,
            capture_output=True,
            text=True
        )
        print(result.stderr)
    except Exception as e:
        raise ValueError(e)

    return str(target_dir)


def checkout(workspace: str, pull_pr: str, branch: str):
    """
    Fetch pr status from GitHub and check out to specific branch
    :param workspace: workspace path
    :param pull_pr: e.g. pull/1234
    :param branch: branch name
    """
    # Validate workspace directory exists
    workspace_path, original_dir = _get_workspace_and_origin(workspace)
    try:
        os.chdir(workspace_path)

        # Fetch all branches first to ensure the branch is available
        fetch_pr = pull_pr + "/head:" + branch
        try:
            subprocess.run(
                ['git', 'fetch', 'origin', fetch_pr],
                check=True,
                capture_output=True,
                text=True
            )
        except Exception:
            # already fetched
            pass

        # Checkout to the specified branch
        subprocess.run(
            ['git', 'checkout', branch],
            check=True,
            capture_output=True,
            text=True
        )

    except subprocess.CalledProcessError as e:
        error_msg = f"Failed to checkout branch '{branch}' in workspace '{workspace}'\n"
        error_msg += f"Error: {e.stderr}\n"
        error_msg += f"Please verify that the branch exists and you have access to it."
        raise RuntimeError(error_msg) from e
    finally:
        # Restore the original working directory
        os.chdir(original_dir)


def git_fetch(workspace: str, remote: str, commit_id: str):
    """
    Fetch a commit from a remote, mainly used for hidden commit
    :param workspace: workspace path
    :param remote: e.g. origin/main
    :param commit_id: commit id
    :return:
    """
    workspace_path, original_dir = _get_workspace_and_origin(workspace)
    try:
        os.chdir(workspace_path)
        # Fetch the commit
        subprocess.run(
            ['git', 'fetch', remote, commit_id],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as e:
        error_msg = f"Failed to fetch commit '{commit_id}' from remote '{remote}' in workspace '{workspace}'\n"
        error_msg += f"Error: {e.stderr}\n"
        error_msg += f"Please verify that the commit exists and you have access to it."
        raise RuntimeError(error_msg) from e
    finally:
        # Restore the original working directory
        os.chdir(original_dir)


def get_pr_title_desc(repo: str, pr_id: str):
    """
    Get the PR title and description
    """
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_id}"
    response = requests.get(url)
    if response.status_code != 200:
        raise ValueError(f"Failed to get PR {pr_id} from {repo}")
    return response.json()["title"], response.json()["body"]

