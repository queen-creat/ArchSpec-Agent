/* 只负责说明状态，不批准来源，也不让浏览器提交维护凭据。 */
globalThis.ArchSpecAdmissionUI = {
  evidenceLabel(evidence) {
    if (evidence.evidence_mode === "HISTORICAL") return "历史记录 · 不代表当前可引用";
    if (evidence.is_citable === true && evidence.evidence_mode === "AUTHORITATIVE") {
      return "来源准入通过 · 项目适用性待复核";
    }
    return "候选资料 · 尚不可作为正式依据";
  },
  searchNotice(response) {
    if (response.admission_notice) return response.admission_notice;
    if (response.results?.length) return "来源准入通过不等于设计合规，项目适用性仍须专业人员复核。";
    const count = response.excluded_documents?.length || 0;
    return `未找到已通过核验的匹配正文${count ? `；${count} 份资料尚未满足准入条件` : ""}。这不代表没有规范要求，可先试查待核验正文并对照原页。`;
  },
  statusText(status) {
    if (!status.migrated) return "独立核验记录尚未建立。已有正文仍可作为候选试查，不能自动成为正式依据。";
    return `来源登记已建立，但登记不等于核验通过。${status.verification_enabled ? "本机人工维护已开启" : "人工核验写入默认关闭"}；现行版本、完整正文与使用条件须分别核对，项目适用性尚未审查。`;
  },
};

if (typeof document !== "undefined") {
  const box = document.querySelector("#governance-status");
  if (box) {
    fetch("/api/standard-governance/status", {cache: "no-store"})
      .then(response => {
        if (!response.ok) throw new Error("状态暂不可读");
        return response.json();
      })
      .then(status => { box.textContent = ArchSpecAdmissionUI.statusText(status); })
      .catch(() => { box.textContent = "核验状态暂不可读。不能据此推定资料已核验，请仅作候选查阅。"; });
  }
}
