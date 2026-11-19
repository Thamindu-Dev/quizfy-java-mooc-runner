import express from "express";
import cors from "cors";
import { gradeSubmission } from "./executor.js";

const app = express();
const PORT = Number(process.env.PORT || 7860);
const MAX_CONCURRENT = Number(process.env.MAX_CONCURRENT || 3);
const MAX_QUEUE = Number(process.env.MAX_QUEUE || 20);

const ALLOWED_ORIGINS = (
  process.env.ALLOWED_ORIGINS || "https://quizfy.zynthlab.com,http://localhost:3000"
)
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

app.use(
  cors({
    origin(origin, cb) {
      if (!origin || ALLOWED_ORIGINS.includes(origin)) return cb(null, true);
      return cb(new Error("CORS origin not allowed"));
    },
    methods: ["GET", "POST", "OPTIONS"],
  })
);
app.use(express.json({ limit: "256kb" }));

// Simple per-IP rate limiter (15 requests per 60s)
const ipHits = new Map();
function rateLimit(req, res, next) {
  const ip = req.headers["x-forwarded-for"]?.toString().split(",")[0].trim() || req.ip || "unknown";
  const now = Date.now();
  const windowMs = 60_000;
  const recent = (ipHits.get(ip) || []).filter((t) => now - t < windowMs);
  if (recent.length >= 15) {
    return res.status(429).json({ status: "error", error: "Too many submissions. Please wait a minute." });
  }
  recent.push(now);
  ipHits.set(ip, recent);
  next();
}

// Concurrency semaphore queue
let activeRuns = 0;
const queue = [];

function enqueue(task) {
  return new Promise((resolve, reject) => {
    if (queue.length >= MAX_QUEUE) {
      return reject(new Error("BUSY"));
    }
    const run = async () => {
      activeRuns++;
      try {
        resolve(await task());
      } catch (err) {
        reject(err);
      } finally {
        activeRuns--;
        if (queue.length > 0) {
          const nextTask = queue.shift();
          nextTask();
        }
      }
    };
    if (activeRuns < MAX_CONCURRENT) {
      run();
    } else {
      queue.push(run);
    }
  });
}

app.get("/health", (_req, res) => {
  res.json({
    ok: true,
    activeRuns,
    queuedRuns: queue.length,
  });
});

app.post("/grade", rateLimit, async (req, res) => {
  const { exerciseId, files } = req.body || {};
  try {
    const result = await enqueue(() => gradeSubmission(exerciseId, files));
    res.json(result);
  } catch (err) {
    if (err.message === "BUSY") {
      return res.status(503).json({ status: "error", error: "Runner is busy. Please retry in a few seconds." });
    }
    res.status(500).json({ status: "error", error: "Internal grading error." });
  }
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`Quizfy Java MOOC Runner listening on port ${PORT}`);
});
