---
title: Quizfy Java MOOC Runner
emoji: ☕
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
---

# Quizfy Java MOOC Runner

Open-source JUnit grading runner and exercise bank for **Quizfy**, adapted from the [University of Helsinki Java Programming MOOC](https://java-programming.mooc.fi/).

## Endpoints

- `GET /health` - Container health & queue status (also wakes up sleeping HF Spaces).
- `POST /grade` - Compiles and runs JUnit tests against submitted Java files.
  ```json
  {
    "exerciseId": "part01-01-sandbox",
    "files": {
      "src/main/java/Sandbox.java": "public class Sandbox { ... }"
    }
  }
  ```

## License

Exercise content and test suites are adapted from the University of Helsinki MOOC.fi Java Programming course and are licensed under **CC BY-NC-SA 4.0** .
