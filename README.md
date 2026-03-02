# ADK-Java-Coding-AgentsWSubagents

This project is a minimal **coding-agent** built with **Google ADK (Python)** that generates/updates a simple **Java** project and iterates in a loop:
1. Create/update JUnit5 test harness + build files  
2. Run unit tests via a shell tool boundary  
3. Improve code based on failures  
4. Stop early when tests pass

The Java project lives in: `adk_coding_agent_workspace/`

1) Python
- Python 3.10+ (you used 3.12, that’s fine)

Create and activate a virtualenv:

2) bash:
python3 -m venv venv
source venv/bin/activate

3) Java (JDK)
Have a JDK17 installed to compile/run Java tests.
must be JDK17

Verify:
java -version
javac -version

4) make sure you have gradle
gradle -version

5) update build.gradle

   
  plugins {
      id 'java'
  }
  
  repositories {
      mavenCentral()
  }
  
  dependencies {
      testImplementation platform('org.junit:junit-bom:5.10.2')
      testImplementation 'org.junit.jupiter:junit-jupiter'
      testRuntimeOnly 'org.junit.platform:junit-platform-launcher'
  }
  
  test {
      useJUnitPlatform()
  }

Run 'gradle clean test --no-daemon' to clean up after making changes

6) Create a .env file in the repo root (same folder as coding_agent_adk.py):

GOOGLE_API_KEY=YOUR_KEY_HERE

Make sure .env is ignored by git (recommended):
Add to .gitignore:

.env

Confirm the script sees the key:
When you run:

python3 coding_agent_adk.py

You should see something like:

API KEY LOADED: True


7) Running agent:

source venv/bin/activate
python3 coding_agent_adk.py
