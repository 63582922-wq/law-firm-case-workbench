"use client";

import { useEffect, useState, type FormEvent } from "react";
import {
  createLocalCase,
  inventoryLocalCaseFolder,
  listLocalCases,
  openLocalCase,
  reconnectLocalCaseFolder,
  selectLocalCaseFolder,
  type LocalCaseFolderSelection,
  type LocalCaseSummary,
} from "@/lib/desktop-bridge";
import {
  loadLocalStandaloneInventoryPage,
  type LocalStandaloneInventoryItem,
} from "@/lib/local-standalone-case-source";
import styles from "./case-workbench.module.css";

type LocalCaseOnboardingProps = {
  onCaseOpened: (caseSummary: LocalCaseSummary) => void;
  initialNotice?: string | null;
};

/**
 * First-run local case flow.  It intentionally does not accept a typed path,
 * does not invoke a scan, and does not treat a cancelled native picker as a
 * successful material connection.
 */
export function LocalStandaloneOnboarding({ onCaseOpened, initialNotice = null }: LocalCaseOnboardingProps) {
  const [selection, setSelection] = useState<LocalCaseFolderSelection | null>(null);
  const [title, setTitle] = useState("");
  const [cases, setCases] = useState<LocalCaseSummary[]>([]);
  const [listState, setListState] = useState<"loading" | "ready" | "blocked">("loading");
  const [notice, setNotice] = useState<string | null>(initialNotice);
  const [busy, setBusy] = useState<"select" | "create" | "open" | null>(null);
  const [openingCaseId, setOpeningCaseId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let active = true;
    void listLocalCases()
      .then((items) => {
        if (!active) return;
        setCases(items);
        setListState("ready");
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setListState("blocked");
        setNotice(reason instanceof Error ? reason.message : "暂时无法读取本机案件目录。");
      });
    return () => { active = false; };
  }, [refreshKey]);

  async function chooseFolder() {
    setBusy("select");
    setNotice(null);
    try {
      const nextSelection = await selectLocalCaseFolder();
      if (nextSelection === null) {
        setNotice("已取消选择；系统没有读取任何文件，也没有建立案件。");
        return;
      }
      setSelection(nextSelection);
      setNotice(`已选择“${nextSelection.displayName}”。下一步请填写案件名称；在你建立案件前，系统不会读取其中的文件。`);
    } catch (reason: unknown) {
      setNotice(reason instanceof Error ? reason.message : "无法打开本机文件夹选择器；没有读取任何文件。");
    } finally {
      setBusy(null);
    }
  }

  async function createCase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selection) return;
    setBusy("create");
    setNotice(null);
    try {
      const created = await createLocalCase({ title, selectionId: selection.selectionId });
      onCaseOpened(created);
    } catch (reason: unknown) {
      setNotice(reason instanceof Error ? reason.message : "本机案件没有建立；当前资料文件夹也没有被读取。");
    } finally {
      setBusy(null);
    }
  }

  async function openExistingCase(caseId: string) {
    setBusy("open");
    setOpeningCaseId(caseId);
    setNotice(null);
    try {
      const caseSummary = await openLocalCase(caseId);
      onCaseOpened(caseSummary);
    } catch (reason: unknown) {
      setNotice(reason instanceof Error ? reason.message : "未能打开该本机案件；没有重新授权或读取资料文件夹。");
    } finally {
      setOpeningCaseId(null);
      setBusy(null);
    }
  }

  return (
    <section className={styles.content} aria-label="从本地资料开始办案">
      <div className={styles.contentTopline}>
        <span>新建或打开案件</span>
        <span className={styles.statusPill}>本机办案</span>
      </div>
      <article className={styles.localOnboardingHero}>
        <div>
          <p className={styles.eyebrow}>从本地资料开始</p>
          <h2>先选择本案资料文件夹，再建立案件</h2>
          <p>资料留在这台电脑。系统只记录你确认的本机案件与资料根，不会把文件夹路径、材料内容或当事人信息发送到外部服务。</p>
        </div>
        <ol className={styles.localOnboardingSteps} aria-label="建案步骤">
          <li className={selection ? styles.localStepDone : styles.localStepCurrent}><span>01</span><strong>选择资料文件夹</strong><small>{selection ? selection.displayName : "使用系统文件夹选择器"}</small></li>
          <li className={selection ? styles.localStepCurrent : ""}><span>02</span><strong>命名并建立案件</strong><small>只保存办案用名称</small></li>
          <li><span>03</span><strong>确认后只读盘点材料</strong><small>建立案件后再单独确认</small></li>
        </ol>
      </article>

      <section className={styles.localCreatePanel} aria-labelledby="local-create-heading">
        <div className={styles.localCreateHeading}>
          <div>
            <p className={styles.eyebrow}>新建本机案件</p>
            <h3 id="local-create-heading">先确认资料文件夹</h3>
          </div>
          <button disabled={busy !== null} onClick={() => void chooseFolder()} type="button">
            {busy === "select" ? "正在打开选择器…" : selection ? "重新选择资料文件夹" : "选择资料文件夹"}
          </button>
        </div>
        <div className={styles.localSelectionState} data-selected={selection ? "true" : "false"}>
          <strong>{selection ? `已选择：${selection.displayName}` : "尚未选择资料文件夹"}</strong>
          <span>{selection ? "尚未读取文件；建立案件后仍需单独确认材料盘点范围。" : "点击上方按钮后，系统只会打开 macOS 文件夹选择器。"}</span>
        </div>
        <form className={styles.localCaseForm} onSubmit={(event) => void createCase(event)}>
          <label>
            <span>案件名称</span>
            <input
              disabled={!selection || busy !== null}
              maxLength={160}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="例如：周雅丽民间借贷纠纷"
              required
              value={title}
            />
          </label>
          <div className={styles.localCreateActions}>
            <button disabled={!selection || busy !== null || title.trim().length < 2} type="submit">
              {busy === "create" ? "正在建立本机案件…" : "建立案件"}
            </button>
            <small>建立成功后才会打开本机案件；名称不等于对方身份、金额、期限或诉讼立场。</small>
          </div>
        </form>
        {notice ? <p className={styles.localNotice} role="status">{notice}</p> : null}
      </section>

      <section className={styles.localCaseList} aria-label="已有本机案件">
        <div className={styles.localListHeading}>
          <div>
            <p className={styles.eyebrow}>已有本机案件</p>
            <h3>{listState === "loading" ? "正在读取本机案件…" : listState === "blocked" ? "暂时无法读取案件目录" : cases.length === 0 ? "还没有本机案件" : "继续办理"}</h3>
          </div>
          {listState === "blocked" ? <button onClick={() => setRefreshKey((value) => value + 1)} type="button">重新读取</button> : null}
        </div>
        {listState === "ready" && cases.length > 0 ? (
          <div className={styles.localCaseRows}>
            {cases.map((item) => (
              <button disabled={busy !== null} key={item.caseId} onClick={() => void openExistingCase(item.caseId)} type="button">
                <span>
                  <strong>{item.title}</strong>
                  <small>资料根：{item.materialRoot.displayName} · {item.inventory ? `已只读盘点 ${item.inventory.totalFiles} 个文件` : "尚未开始材料盘点"}</small>
                </span>
                <em>{openingCaseId === item.caseId ? "正在打开…" : "打开"}</em>
              </button>
            ))}
          </div>
        ) : listState === "ready" ? <p className={styles.localEmpty}>本机案件会保存在当前电脑；建立后可以从这里继续办理。</p> : null}
      </section>

      <details className={styles.localFirmOption}>
        <summary>需要接入律所协作、团队权限或受管资料库？</summary>
        <p>这是可选的后续设置，不影响在本机建立和打开个人案件。需要时可在<a href="/security">工作台设置</a>中另行接入。</p>
      </details>
    </section>
  );
}

type LocalStandaloneCaseHomeProps = {
  caseSummary: LocalCaseSummary;
  onCaseUpdated: (caseSummary: LocalCaseSummary) => void;
  onShowCaseList: () => void;
};

export function LocalStandaloneCaseHome({ caseSummary, onCaseUpdated, onShowCaseList }: LocalStandaloneCaseHomeProps) {
  const [replacementSelection, setReplacementSelection] = useState<LocalCaseFolderSelection | null>(null);
  const [inventorySelection, setInventorySelection] = useState<LocalCaseFolderSelection | null>(null);
  const [inventoryConfirmed, setInventoryConfirmed] = useState(false);
  const [busy, setBusy] = useState<"select-reconnect" | "reconnect" | "select-inventory" | "inventory" | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [inventoryNotice, setInventoryNotice] = useState<string | null>(null);
  const [inventoryItems, setInventoryItems] = useState<LocalStandaloneInventoryItem[]>([]);
  const [inventoryNextOffset, setInventoryNextOffset] = useState<number | null>(null);
  const [inventoryLoadState, setInventoryLoadState] = useState<"idle" | "loading" | "ready" | "blocked">("idle");

  useEffect(() => {
    let active = true;
    void Promise.resolve().then(async () => {
      if (!active) return;
      const inventory = caseSummary.inventory;
      if (inventory === null) {
        setInventoryItems([]);
        setInventoryNextOffset(null);
        setInventoryLoadState("idle");
        return;
      }
      setInventoryLoadState("loading");
      setInventoryNotice(null);
      try {
        const page = await loadLocalStandaloneInventoryPage(caseSummary.caseId, inventory);
        if (!active) return;
        setInventoryItems(page.items);
        setInventoryNextOffset(page.nextOffset);
        setInventoryLoadState("ready");
      } catch (reason: unknown) {
        if (!active) return;
        setInventoryLoadState("blocked");
        setInventoryNotice(reason instanceof Error ? reason.message : "本机材料清单暂时无法读取。已盘点摘要仍保留。");
      }
    });
    return () => { active = false; };
  }, [caseSummary.caseId, caseSummary.inventory]);

  async function chooseReplacementFolder() {
    setBusy("select-reconnect");
    setNotice(null);
    try {
      const nextSelection = await selectLocalCaseFolder();
      if (nextSelection === null) {
        setNotice("已取消选择；当前案件仍关联原资料文件夹，系统没有读取任何文件。");
        return;
      }
      setReplacementSelection(nextSelection);
      setNotice(`已选择“${nextSelection.displayName}”。请确认后再更换本案资料根；此操作不会读取文件。`);
    } catch (reason: unknown) {
      setNotice(reason instanceof Error ? reason.message : "无法打开本机文件夹选择器；当前关联没有改变。");
    } finally {
      setBusy(null);
    }
  }

  async function reconnectFolder() {
    if (!replacementSelection) return;
    setBusy("reconnect");
    setNotice(null);
    try {
      const updated = await reconnectLocalCaseFolder({ caseId: caseSummary.caseId, selectionId: replacementSelection.selectionId });
      onCaseUpdated(updated);
      setReplacementSelection(null);
      setInventorySelection(null);
      setInventoryConfirmed(false);
      setNotice(`“${updated.materialRoot.displayName}”已关联到本案。尚未读取、上传或分析其中的文件。`);
    } catch (reason: unknown) {
      setNotice(reason instanceof Error ? reason.message : "本案资料根未更换；当前关联保持不变。");
    } finally {
      setBusy(null);
    }
  }

  async function chooseInventoryFolder() {
    setBusy("select-inventory");
    setInventoryNotice(null);
    try {
      const nextSelection = await selectLocalCaseFolder();
      if (nextSelection === null) {
        setInventoryNotice("已取消选择；系统没有开始盘点，也没有读取任何文件。");
        return;
      }
      if (nextSelection.rootFingerprint !== caseSummary.materialRoot.rootFingerprint) {
        setInventorySelection(null);
        setInventoryConfirmed(false);
        setInventoryNotice(`“${nextSelection.displayName}”不是当前本案关联的资料根。请先在下方“更换资料文件夹”中明确关联后，再开始盘点。`);
        return;
      }
      setInventorySelection(nextSelection);
      setInventoryConfirmed(false);
      setInventoryNotice(`已确认所选文件夹与“${caseSummary.materialRoot.displayName}”一致。勾选确认后，系统才会开始本机只读盘点。`);
    } catch (reason: unknown) {
      setInventoryNotice(reason instanceof Error ? reason.message : "无法打开本机文件夹选择器；没有开始盘点。 ");
    } finally {
      setBusy(null);
    }
  }

  async function inventoryFolder() {
    if (!inventorySelection || !inventoryConfirmed) return;
    setBusy("inventory");
    setInventoryNotice(null);
    try {
      const updated = await inventoryLocalCaseFolder({ caseId: caseSummary.caseId, selectionId: inventorySelection.selectionId });
      if (updated.inventory === null || updated.stage !== "MATERIALS_INVENTORIED") {
        throw new Error("本机材料盘点未形成有效回执；不显示任何盘点结果。");
      }
      onCaseUpdated(updated);
      setInventorySelection(null);
      setInventoryConfirmed(false);
      setInventoryNotice(`只读盘点已完成：${updated.inventory.totalFiles} 个文件，${formatBytes(updated.inventory.totalBytes)}。下面显示本机返回的文件清单。`);
    } catch (reason: unknown) {
      setInventoryNotice(reason instanceof Error ? reason.message : "本机材料盘点未完成；没有显示任何盘点结果。");
    } finally {
      setBusy(null);
    }
  }

  async function loadMoreInventoryItems() {
    if (!caseSummary.inventory || inventoryNextOffset === null || inventoryLoadState === "loading") return;
    setInventoryLoadState("loading");
    setInventoryNotice(null);
    try {
      const page = await loadLocalStandaloneInventoryPage(caseSummary.caseId, caseSummary.inventory, inventoryNextOffset);
      setInventoryItems((current) => current.concat(page.items));
      setInventoryNextOffset(page.nextOffset);
      setInventoryLoadState("ready");
    } catch (reason: unknown) {
      setInventoryLoadState("blocked");
      setInventoryNotice(reason instanceof Error ? reason.message : "本机材料后续清单暂时无法读取；已显示项目仍保留。");
    }
  }

  async function reloadInventoryItems() {
    if (!caseSummary.inventory || inventoryLoadState === "loading") return;
    setInventoryLoadState("loading");
    setInventoryNotice(null);
    try {
      const page = await loadLocalStandaloneInventoryPage(caseSummary.caseId, caseSummary.inventory);
      setInventoryItems(page.items);
      setInventoryNextOffset(page.nextOffset);
      setInventoryLoadState("ready");
    } catch (reason: unknown) {
      setInventoryLoadState("blocked");
      setInventoryNotice(reason instanceof Error ? reason.message : "本机材料清单暂时无法读取。已盘点摘要仍保留。");
    }
  }

  return (
    <section className={styles.content} aria-label="本机案件办案首页">
      <div className={styles.contentTopline}>
        <span>办案首页</span>
        <span className={styles.statusPill}>本机案件已打开</span>
      </div>
      <article className={styles.localCaseHero}>
        <div>
          <p className={styles.eyebrow}>本机案件</p>
          <h2>{caseSummary.title}</h2>
          <p>{caseSummary.inventory ? "本机只读盘点已形成材料清单。正式的证据页整理、案情、法律、利息和应诉材料仍只会在相应能力真正启用后开放。" : "资料根已关联，但系统尚未开始读取、上传、整理或生成任何材料。先完成实际的资料盘点后，才能显示真实的材料数量和清单。"}</p>
        </div>
        <dl>
          <div><dt>资料根</dt><dd>{caseSummary.materialRoot.displayName}</dd></div>
          <div><dt>当前状态</dt><dd>{caseSummary.inventory ? "已完成只读盘点" : "等待材料盘点"}</dd></div>
          <div><dt>案件版本</dt><dd>版本 {caseSummary.matterVersion}</dd></div>
        </dl>
      </article>

      <section className={styles.localInventoryPanel} aria-label="本机只读盘点材料">
        <header>
          <div>
            <p className={styles.eyebrow}>材料盘点</p>
            <h3>{caseSummary.inventory ? "本机只读盘点结果" : "开始只读盘点材料"}</h3>
          </div>
          <span>{caseSummary.inventory ? "已取得本机回执" : "尚未读取文件"}</span>
        </header>
        {caseSummary.inventory ? (
          <>
            <dl className={styles.localInventorySummary}>
              <div><dt>已盘点文件</dt><dd>{caseSummary.inventory.totalFiles} 个</dd></div>
              <div><dt>合计大小</dt><dd>{formatBytes(caseSummary.inventory.totalBytes)}</dd></div>
              <div><dt>跳过的符号链接</dt><dd>{caseSummary.inventory.skippedSymlinks} 个</dd></div>
              <div><dt>盘点时间</dt><dd>{formatLocalDateTime(caseSummary.inventory.scannedAt)}</dd></div>
            </dl>
            <div className={styles.localInventoryListHeading}>
              <strong>已盘点的文件</strong>
              <span>{inventoryLoadState === "loading" ? "正在读取清单…" : `已显示 ${inventoryItems.length} 个项目`}</span>
            </div>
            {inventoryItems.length > 0 ? (
              <div className={styles.localInventoryRows}>
                {inventoryItems.map((item) => (
                  <article key={`${item.relativePath}:${item.sha256}`}>
                    <span>{item.detectedKind}</span>
                    <strong title={item.relativePath}>{item.relativePath}</strong>
                    <small>{formatBytes(item.byteSize)}</small>
                  </article>
                ))}
              </div>
            ) : inventoryLoadState === "ready" ? <p className={styles.localEmpty}>该资料根中没有可盘点的常规文件。</p> : null}
            <div className={styles.localInventoryActions}>
              {inventoryNextOffset !== null ? <button disabled={inventoryLoadState === "loading"} onClick={() => void loadMoreInventoryItems()} type="button">{inventoryLoadState === "loading" ? "正在读取…" : "载入更多文件"}</button> : null}
              {inventoryLoadState === "blocked" ? <button onClick={() => void reloadInventoryItems()} type="button">重新读取清单</button> : null}
            </div>
          </>
        ) : (
          <div className={styles.localInventoryStart}>
            <p>盘点会由本机服务只读检查当前资料根、形成文件数、大小、类型与哈希清单。不会修改、删除、上传或生成任何材料。</p>
            <div className={styles.localInventoryActions}>
              <button disabled={busy !== null} onClick={() => void chooseInventoryFolder()} type="button">{busy === "select-inventory" ? "正在打开选择器…" : "选择本案资料文件夹"}</button>
              {inventorySelection ? <button disabled={busy !== null || !inventoryConfirmed} onClick={() => void inventoryFolder()} type="button">{busy === "inventory" ? "正在只读盘点…" : "开始只读盘点材料"}</button> : null}
            </div>
            {inventorySelection ? (
              <label className={styles.localInventoryConsent}>
                <input checked={inventoryConfirmed} disabled={busy !== null} onChange={(event) => setInventoryConfirmed(event.target.checked)} type="checkbox" />
                <span>我确认“{inventorySelection.displayName}”是本案资料根，并同意仅在本机进行只读盘点；此操作不代表已确认任何事实、证据相关性或法律结论。</span>
              </label>
            ) : null}
          </div>
        )}
        {inventoryNotice ? <p className={styles.localNotice} role="status">{inventoryNotice}</p> : null}
      </section>

      <section className={styles.localCapabilityPanel} aria-label="本机案件可用能力">
        <header>
          <div>
            <p className={styles.eyebrow}>当前可用范围</p>
            <h3>已完成本机建案与资料根关联</h3>
          </div>
          <button onClick={onShowCaseList} type="button">切换案件</button>
        </header>
        <div className={styles.localCapabilityRows}>
          <article><span>01</span><div><strong>本机案件与资料根</strong><small>已可建立、打开和更换关联；不显示或保存文件夹绝对路径。</small></div><em>已可用</em></article>
          <article><span>02</span><div><strong>资料盘点与逐页整理</strong><small>{caseSummary.inventory ? `已取得 ${caseSummary.inventory.totalFiles} 个文件的只读清单；逐页证据整理尚未开放。` : "只有取得真实本机盘点回执后才会显示材料数量，不显示虚构结果。"}</small></div><em>{caseSummary.inventory ? "盘点已完成" : "待盘点"}</em></article>
          <article><span>03</span><div><strong>案情、法律、利息与提交材料</strong><small>只能建立在已盘点并经律师确认的资料上；当前本机基础案卷不把空数据或演示案情当作真实结果。</small></div><em>待能力启用</em></article>
        </div>
      </section>

      <section className={styles.localReconnectPanel} aria-label="更换本案资料文件夹">
        <div>
          <p className={styles.eyebrow}>资料根关联</p>
          <h3>需要更换本案资料文件夹？</h3>
          <p>先在系统选择器中重新选择，再明确确认关联。更换关联不会读取或删除任何文件。</p>
        </div>
        <div className={styles.localReconnectActions}>
          <button disabled={busy !== null} onClick={() => void chooseReplacementFolder()} type="button">
            {busy === "select-reconnect" ? "正在打开选择器…" : "选择新的资料文件夹"}
          </button>
          {replacementSelection ? <button disabled={busy !== null} onClick={() => void reconnectFolder()} type="button">确认关联“{replacementSelection.displayName}”</button> : null}
        </div>
        {notice ? <p className={styles.localNotice} role="status">{notice}</p> : null}
      </section>
    </section>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatLocalDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间回执无效";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function LocalStandaloneFeatureUnavailable({
  caseSummary,
  featureLabel,
  onReturnHome,
}: {
  caseSummary: LocalCaseSummary;
  featureLabel: string;
  onReturnHome: () => void;
}) {
  return (
    <section className={styles.content} aria-label={`${featureLabel}尚未开放`}>
      <div className={styles.contentTopline}><span>{featureLabel}</span><span className={styles.statusPill}>{caseSummary.inventory ? "材料已盘点" : "等待材料盘点"}</span></div>
      <article className={styles.localUnavailableHero}>
        <div>
          <p className={styles.eyebrow}>本机案件已打开</p>
          <h2>{caseSummary.inventory ? `“${featureLabel}”需要对应的正式工作能力` : `先建立真实的材料盘点，再使用“${featureLabel}”`}</h2>
          <p>{caseSummary.inventory ? `“${caseSummary.title}”已取得 ${caseSummary.inventory.totalFiles} 个文件的本机只读清单，但尚未启用“${featureLabel}”所需的正式证据、事实、法律或文书工作链。系统不会用空数据或演示结果代替。` : `“${caseSummary.title}”当前只完成资料根关联。系统尚未读取文件，因此不能显示证据、案情、法律依据、利息或应诉材料的示例结果。`}</p>
        </div>
        <button onClick={onReturnHome} type="button">返回本机案件</button>
      </article>
    </section>
  );
}
