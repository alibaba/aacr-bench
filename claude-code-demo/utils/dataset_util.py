from .model import PRDataItem
import json

def get_gitrepo_pr_id(pr_url: str) -> tuple[str, str]:
    """
    Get the git repo and pr_id from the pr url
    :param pr_url: e.g. "https://github.com/gofr-dev/gofr/pull/1681"
    """
    pull_index = pr_url.find("pull")
    if pull_index <= 0:
        raise ValueError("Invalid pr url")
    repo_url = pr_url[:pull_index - 1] + ".git"
    pr_id = pr_url[pull_index + 5:]
    return repo_url, pr_id


def load_dataset(path: str) -> list[PRDataItem]:
    """
    Load the dataset from the given path
    :param path: the path to the dataset
    """
    with open(path, "r", encoding="utf-8") as f:
        return [PRDataItem(**item) for item in json.load(f)]
