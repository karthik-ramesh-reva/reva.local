import subprocess

def run_shell_script():
    # Use osascript to open a new terminal and run the shell script
    script = """
    tell application "Terminal"
        do script "cd {} && ./start.sh"
        activate
    end tell
    """.format(subprocess.os.getcwd())
    subprocess.run(["osascript", "-e", script])

if __name__ == "__main__":
    run_shell_script()