import { test } from "node:test";
import assert from "node:assert/strict";
import { JOB_OUTPUT_LIMIT, mergeIncrementalJobOutput, type JobOutputCache } from "./job-output.ts";

test("appends incremental output to the followed job", () => {
  const cache: JobOutputCache = new Map();
  const [full] = mergeIncrementalJobOutput([{ id: 7, output: "a\nb\n", output_from: 0, output_total: 4 }], cache);
  assert.equal(full.output, "a\nb\n");

  const [next] = mergeIncrementalJobOutput([{ id: 7, output: "c\n", output_from: 4, output_total: 6 }], cache);
  assert.equal(next.output, "a\nb\nc\n");
  assert.deepEqual(cache.get(7), { output: "a\nb\nc\n", total: 6 });
});

test("a delta that does not match the cache forces a full reload next time", () => {
  const cache: JobOutputCache = new Map([[7, { output: "a\n", total: 2 }]]);
  const [job] = mergeIncrementalJobOutput([{ id: 7, output: "z\n", output_from: 9, output_total: 11 }], cache);
  assert.equal(job.output, "a\n");
  assert.equal(cache.has(7), false);
});

test("jobs without detail keep their compact payload and the cache follows the detailed job", () => {
  const cache: JobOutputCache = new Map([[7, { output: "old", total: 3 }]]);
  const jobs = mergeIncrementalJobOutput([
    { id: 8, output: "new", output_from: 0, output_total: 3 },
    { id: 7, output: "" },
  ], cache);
  assert.equal(jobs[1].output, "");
  assert.deepEqual([...cache.keys()], [8]);
});

test("merged output stays bounded", () => {
  const cache: JobOutputCache = new Map([[1, { output: "x".repeat(JOB_OUTPUT_LIMIT), total: JOB_OUTPUT_LIMIT }]]);
  const [job] = mergeIncrementalJobOutput([{ id: 1, output: "fin", output_from: JOB_OUTPUT_LIMIT, output_total: JOB_OUTPUT_LIMIT + 3 }], cache);
  assert.equal(job.output?.length, JOB_OUTPUT_LIMIT);
  assert.ok(job.output?.endsWith("fin"));
});
