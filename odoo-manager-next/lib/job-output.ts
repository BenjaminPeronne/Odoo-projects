export const JOB_OUTPUT_LIMIT = 120_000;

export type JobOutputCache = Map<number, { output: string; total: number }>;

type IncrementalJob = {
  id: number;
  output?: string;
  // Présents sur le job détaillé : output_from > 0 signifie que `output` n'est que la suite.
  output_from?: number;
  output_total?: number;
};

// Recompose la sortie du job suivi à partir des suites envoyées par /api/jobs?output_from=N.
export function mergeIncrementalJobOutput<T extends IncrementalJob>(items: T[], cache: JobOutputCache): T[] {
  return items.map((job) => {
    if (job.output_from === undefined) return job;
    const cached = cache.get(job.id);
    let output = job.output || "";
    if (job.output_from > 0) {
      if (!cached || cached.total !== job.output_from) {
        // Suite inutilisable (autre rafraîchissement entre-temps) : la prochaine requête redemande tout.
        cache.delete(job.id);
        return { ...job, output: cached?.output ?? "" };
      }
      output = (cached.output + output).slice(-JOB_OUTPUT_LIMIT);
    }
    cache.clear();
    cache.set(job.id, { output, total: job.output_total ?? output.length });
    return { ...job, output };
  });
}
