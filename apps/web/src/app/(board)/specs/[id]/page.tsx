import { SpecStudioPage } from "@/components/spec-studio/spec-studio-page";

/**
 * `/specs/{id}` — a dedicated, deep-linkable Spec Studio for one spec,
 * defaulting to Guided mode.
 */
export default async function SpecRoute({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <SpecStudioPage specId={id} />;
}
