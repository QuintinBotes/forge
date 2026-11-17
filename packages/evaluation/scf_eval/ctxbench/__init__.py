"""ctxbench: SCF's context benchmark, on lore's published method (spec J §2.4, §2.4.1).

Pure harness logic with no ``scf_api`` imports: the bank and corpus (:mod:`.bank`), its
prompts (:mod:`.prompts`), one isolated ``claude --print`` run (:mod:`.claude`),
the judge (:mod:`.judge`), lore's scoring (:mod:`.score`), the model-free
evidence proxy (:mod:`.proxy`), the report (:mod:`.report`) and the gates
(:mod:`.gate`). ``scf-cli ctxbench`` wires them to the database and the API.
"""
