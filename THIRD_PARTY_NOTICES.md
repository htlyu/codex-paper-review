# Third-party notices

## ScholarPeer prompt material

The ScholarPeer-derived role prompts in [paper_review_service/prompts](paper_review_service/prompts), as identified in the mapping below, adapt material from Appendix G of the following work:

**Palash Goyal, Mihir Parmar, Yiwen Song, Hamid Palangi, Tomas Pfister, and Jinsung Yoon. _ScholarPeer: A Multi-Agent Framework for Automated Peer Review_. arXiv:2601.22638v2, 9 May 2026.**

The title above appears in the v2 PDF and HTML body. The arXiv abstract record titles the same version _ScholarPeer: A Context-Aware Multi-Agent Framework for Automated Peer Review_.

- Source and version record: [arXiv:2601.22638v2](https://arxiv.org/abs/2601.22638v2).
- Original prompt material: [v2 PDF, Appendix G, pages 32–39](https://arxiv.org/pdf/2601.22638v2#page=32).
- Additional source: [v2 HTML](https://arxiv.org/html/2601.22638v2).
- Original material license, as linked from the arXiv record: [Creative Commons Attribution 4.0 International (CC BY 4.0)](https://creativecommons.org/licenses/by/4.0/), with [full legal text](https://creativecommons.org/licenses/by/4.0/legalcode).

**Changes have been made.** Codex Paper Review contributors translated and rewrote the role instructions for an evidence-based author self-check service; changed the input/output contracts to local files and structured JSON; added page and source tracking, uncertainty handling, and a separate countercheck stage; and implemented their own stage orchestration. The adaptation removes fixed literature/question quotas, venue whitelists, assumptions of author concealment, automatic high-novelty conclusions from unsuccessful searches, review scores, and acceptance/rejection recommendations. Appendix H evaluation prompts are not included in the user-paper review workflow.

See [the source-to-role mapping and detailed adaptation record](docs/scholarpeer-prompts.md). The original authors retain rights in their material. Please retain this attribution, source and license links, and the modification notice when redistributing the adapted third-party material.

This is an independent adaptation. It is not an official ScholarPeer implementation or an official PAT prompt release. No endorsement by the authors or their institutions is implied, and this project makes no claim to reproduce the original code, full experimental configuration, or reported performance. The CC BY 4.0 notice identifies the license of the referenced third-party material; it does not establish a license for unrelated code in this repository.
