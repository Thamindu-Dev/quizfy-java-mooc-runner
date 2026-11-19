FROM eclipse-temurin:11-jdk-jammy

# Install Node.js 20 LTS
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces requires running as non-root UID 1000
RUN useradd -m -u 1000 user

WORKDIR /app

# Install Node dependencies
COPY package.json ./
RUN npm install --omit=dev

# Copy libs and compile JsonTestRunner into /app/libs/runner.jar
COPY libs/ ./libs/
COPY runner/JsonTestRunner.java ./runner/JsonTestRunner.java
RUN mkdir -p /tmp/runner-build \
    && javac -cp "/app/libs/*" -d /tmp/runner-build /app/runner/JsonTestRunner.java \
    && jar cf /app/libs/runner.jar -C /tmp/runner-build . \
    && rm -rf /tmp/runner-build

# Copy backend code, security policy, and MOOC tests
COPY sandbox.policy ./sandbox.policy
COPY src/ ./src/
COPY tests/ ./tests/

RUN chown -R user:user /app

USER user
EXPOSE 7860

ENV PORT=7860
ENV NODE_ENV=production

CMD ["node", "src/server.js"]
