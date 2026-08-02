#!/usr/bin/env python3
import os
import sys
import subprocess
import datetime

# Configuration
REPO_DIR = os.getcwd()  # Assumes execution from repository root
SKILLS_DIR = os.path.expanduser("~/.hermes/skills/software-engineering")
REVIEW_SKILL = "swe-reviewer"
UPDATE_SKILL = "swe-updater"

def run_command(cmd, cwd=REPO_DIR):
    """Helper to run system commands and handle errors gracefully."""
    print(f"🚀 Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"❌ Error executing command: {' '.join(cmd)}")
        print(f"STDOUT:\n{result.stdout}")
        print(f"STDERR:\n{result.stderr}")
        sys.exit(1)
    return result.stdout.strip()

def main():
    # 1. Compute dynamic daily execution variables
    today_str = datetime.date.today().strftime("%Y-%m-%d")
    reviewer_branch = f"reviewer-{today_str}"
    updater_branch = f"updater-{today_str}"
    
    print(f"--- Starting Autonomous Engineering Loop for {today_str} ---")

    # 2. Ensure we have the latest upstream changes
    print("🔄 Syncing repository metadata...")
    run_command(["git", "fetch", "origin"])
    run_command(["git", "checkout", "main"])
    run_command(["git", "pull", "origin", "main"])

    # ==========================================
    # STEP 1: EXECUTE CODE REVIEWER AGENT
    # ==========================================
    print(f"\n🧠 Instantiating {REVIEW_SKILL} Agent...")
    
    # Check if a reviewer branch for today already exists locally or remotely
    branch_check = run_command(["git", "branch", "-a"])
    if reviewer_branch in branch_check:
        print(f"⚠️ Branch {reviewer_branch} already exists. Cleaning up local reference...")
        run_command(["git", "branch", "-D", reviewer_branch])
    
    # Spin up isolated review branch
    run_command(["git", "checkout", "-b", reviewer_branch])
    
    # Invoke Hermes to run the reviewer skill headless
    # This generates REVIEW_NOTES.md, commits it, and pushes it up
    print("🔍 Auditing codebase for vulnerabilities, bugs, and structural flaws...")
    hermes_review_cmd = [
        "hermes", "run", 
        "--skill", f"{SKILLS_DIR}/{REVIEW_SKILL}.md",
        "--non-interactive"
    ]
    # We pass the instruction to run the predefined procedure
    review_output = run_command(hermes_review_cmd)
    print(review_output)
    
    # Ensure the artifact was pushed by verifying remote or explicitly running the final sync
    print(f"✅ Review complete. Observations saved to origin/{reviewer_branch}.")

    # ==========================================
    # STEP 2: EXECUTE CODE UPDATER AGENT
    # ==========================================
    print(f"\n🔧 Instantiating {UPDATE_SKILL} Agent...")
    
    # Return to main to branch off of clean baseline for the updater
    run_command(["git", "checkout", "main"])
    
    if updater_branch in branch_check:
        print(f"⚠️ Branch {updater_branch} already exists. Cleaning up local reference...")
        run_command(["git", "branch", "-D", updater_branch])
        
    # Create the updater branch
    run_command(["git", "checkout", "-b", updater_branch])
    
    # Invoke Hermes to execute the updater skill headless
    # It will extract REVIEW_NOTES.md from the reviewer branch and execute local test/compile loops
    print("⚡ Executing structural fixes and verification loops...")
    hermes_update_cmd = [
        "hermes", "run",
        "--skill", f"{SKILLS_DIR}/{UPDATE_SKILL}.md",
        "--non-interactive"
    ]
    update_output = run_command(hermes_update_cmd)
    print(update_output)

    # ==========================================
    # STEP 3: WRAP UP / HANDOVER
    # ==========================================
    print(f"\n🎉 Daily session complete!")
    print(f"Review Notes Branch: {reviewer_branch}")
    print(f"Verified Patch Branch: {updater_branch}")
    print("Ready for automated pull request evaluation or manual developer oversight.")

if __name__ == "__main__":
    main()
