import { LeaderboardView } from "@/components/benchmarks/leaderboard-view";

/**
 * Benchmark leaderboard (F35) — browse published benchmark suites and their
 * ranked, verified-first submissions. Backed by the unauthenticated, typed
 * `/public` router.
 */
export default function LeaderboardPage() {
  return <LeaderboardView />;
}
