// 终末地助手 — 陪玩辅助面板（Hosted TSX，state/action 由宿主桥接）
//
// 三项参数（需求契约）：
//   assist_mode                 宏观建议 / 精细建议          默认 宏观建议
//   confidence_threshold        0.70 / 0.80 / 0.85 / 0.90    默认 0.85
//   consecutive_fail_threshold  2 / 3 / 5                   默认 3
//
// 约定（plugins/DESIGN.md）：
//   - 一切宿主能力走 props.*（禁止裸全局调用；官方 check-hosted-tsx 关卡会拦）
//   - Button.onClick 的返回类型必须是 void
//   - Select 在 .neko-card（overflow: hidden）内不会被裁：真 UI kit 用的是
//     原生 <select>，下拉由系统绘制，无需 overflow: visible
import {
  Page, Card, Stack, Text, Button, ButtonGroup, Divider, Alert, Field, Select,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"

type Advice = {
  signal?: string
  label?: string
  confidence?: number
  macro?: string
  fine?: string | null
  fine_suppressed_reason?: string | null
  consecutive_count?: number
}

// 一套配置档（A/B）的三项参数值
type ProfileVals = {
  assist_mode?: string
  confidence_threshold?: number
  consecutive_fail_threshold?: number
}

type AssistState = {
  running?: boolean
  enabled?: boolean
  reason?: string
  source?: string
  last_error?: string
  config?: {
    assist_mode?: string
    confidence_threshold?: number
    consecutive_fail_threshold?: number
    // 双配置（A/B 档）：缺省时面板回落到上面的扁平值，老配置不受影响
    assist_profile?: string
    profiles?: Record<string, ProfileVals>
  }
  counters?: Record<string, number>
  recent?: Advice[]
}

// 三个候选项与 plugin.toml / Python 侧共用同一套取值。
// ⚠️ 置信度必须补足两位小数：Python 侧给的是 float 0.7，而原生 <select> 的选中
// 判定是 `String(props.value) === String(option.value)`（ui-kit runtime.js:1389），
// `String(0.7)` 得到 "0.7" ≠ 选项 "0.70"，会静默回落到第一项。
const MODE_OPTIONS = ["宏观建议", "精细建议"]
const CONFIDENCE_OPTIONS = ["0.70", "0.80", "0.85", "0.90"]
const CONSECUTIVE_OPTIONS = ["2", "3", "5"]
// 双配置档位：两套独立参数，运行时热切换（改哪档由 set 的 profile 决定）
const PROFILE_OPTIONS = ["A", "B"]

const asConfidence = (v: unknown): string => {
  const n = Number(v)
  return Number.isFinite(n) ? n.toFixed(2) : String(v ?? "")
}

export default function MaaEndAssistPanel(props: PluginSurfaceProps<AssistState>) {
  const { state } = props
  const safe: AssistState = state || {}
  const cfg = safe.config || {}
  const counters = safe.counters || {}
  const recent: Advice[] = Array.isArray(safe.recent) ? safe.recent : []

  // 双配置：当前档缺省 A；档内取值优先用 profiles[档]，缺失则回落扁平值（老配置不受影响）
  const activeProfile = String(cfg.assist_profile ?? "A").toUpperCase()
  const profileVals: ProfileVals = (cfg.profiles || {})[activeProfile] || {}

  const selectedMode = String(profileVals.assist_mode ?? cfg.assist_mode ?? "宏观建议")
  const selectedConfidence = asConfidence(
    profileVals.confidence_threshold ?? cfg.confidence_threshold ?? 0.85)
  const selectedConsecutive = String(
    profileVals.consecutive_fail_threshold ?? cfg.consecutive_fail_threshold ?? 3)

  // 动态切换：写回配置并即时生效（Python 侧重建辅助服务，无需重启插件）
  const setParam = (key: string, value: string): void => {
    // 数字参数按数字传，避免 Python 侧再解析字符串
    const num = Number(value)
    const payload: Record<string, unknown> = {
      [key]: key === "assist_mode" ? value : (Number.isFinite(num) ? num : value),
      profile: activeProfile,          // 改动只写进当前档，另一档不受影响
    }
    void props.api.call("ef_assist", { action: "set", ...payload }, { timeoutMs: 8000 })
  }

  const switchProfile = (value: unknown): void => {
    void props.api.call(
      "ef_assist", { action: "set", profile: String(value) }, { timeoutMs: 8000 })
  }

  // 按所选"算法"自动决定要显示哪些调参项：
  // 宏观建议只按失败类别给结论，不涉及置信度门槛 → 不显示该项。
  const showConfidence = selectedMode === "精细建议"

  return (
    <Page title="终末地助手 · 陪玩辅助">
      {/* tone（不是 kind）—— 真 SDK index.d.ts:236 的 Alert 只认 tone/message */}
      <Alert tone="info">
        陪聊默认开启；卡关辅助只读订阅本地事件流（ZMDLogs / logs 目录），
        不点击、不注入、不接管游戏。
      </Alert>

      <Card title="辅助参数">
        <Stack>
          <Field
            label="配置档（A/B）"
            help="两套独立参数，切换即时生效；改参数只影响当前档，另一档保持原样"
          >
            <Select
              value={activeProfile}
              options={PROFILE_OPTIONS}
              onChange={switchProfile}
            />
          </Field>

          <Field label="建议详细程度" help="宏观建议只说失败原因类别；精细建议额外给可执行动作">
            <Select
              value={selectedMode}
              options={MODE_OPTIONS}
              onChange={(v: unknown) => setParam("assist_mode", String(v))}
            />
          </Field>

          {/* 按算法动态生成：宏观建议不涉及置信度门槛，就不显示这一项 */}
          {showConfidence ? (
            <Field
              label="精细建议置信度门槛"
              help="归因置信度低于此值时不给精细建议（会如实说明原因）"
            >
              <Select
                value={selectedConfidence}
                options={CONFIDENCE_OPTIONS}
                onChange={(v: unknown) => setParam("confidence_threshold", String(v))}
              />
            </Field>
          ) : null}

          <Field label="连续失败门槛" help="连续失败几次算卡关并主动提醒">
            <Select
              value={selectedConsecutive}
              options={CONSECUTIVE_OPTIONS}
              onChange={(v: unknown) => setParam("consecutive_fail_threshold", String(v))}
            />
          </Field>

          <Text>
            当前档 {activeProfile}：{selectedMode}
            {showConfidence ? ` ｜ 门槛 ${selectedConfidence}` : ""}
            {" ｜ "}连续 {selectedConsecutive} 次
          </Text>
          <Text>改动即时生效，无需重启插件；A/B 两档互不影响。</Text>
        </Stack>
      </Card>

      <Card title="运行状态">
        <Stack>
          <Text>
            状态：{safe.running ? "运行中" : "待命"}
            {safe.enabled === false ? "（已关闭）" : ""}
            {safe.source ? ` ｜ 数据源：${safe.source}` : ""}
          </Text>
          {safe.reason ? <Text>说明：{safe.reason}</Text> : null}
          {safe.last_error ? <Text>⚠️ {safe.last_error}</Text> : null}
          <Divider />
          <Text>
            轮询 {counters.polls ?? 0} ｜ 事件 {counters.events ?? 0} ｜ 战斗 {counters.battles ?? 0}
            {" ｜ "}卡关 {counters.stuck ?? 0} ｜ 推送 {counters.pushed ?? 0}
            {" ｜ "}异常 {counters.errors ?? 0}
          </Text>
          <ButtonGroup>
            <Button
              onClick={(): void => {
                void props.api.call("ef_assist", { action: "check" }, { timeoutMs: 20000 })
              }}
            >
              立即检查一次
            </Button>
            <Button
              onClick={(): void => {
                void props.api.call("ef_assist", { action: "state" }, { timeoutMs: 8000 })
              }}
            >
              刷新
            </Button>
          </ButtonGroup>
        </Stack>
      </Card>

      <Card title="最近建议">
        <Stack>
          {recent.length === 0 && (
            <Text>
              暂无。开一局游戏，卡关时这里会出现建议；也可以用对话里的
              「为什么打不过」让我直接看日志。
            </Text>
          )}
          {recent.map((a, i) => (
            <Stack key={i}>
              <Text>
                [{a.signal ?? "-"} · {a.label ?? "-"} · 置信度 {a.confidence ?? "-"}]
              </Text>
              <Text>{a.macro ?? ""}</Text>
              {a.fine ? <Text>精细建议：{a.fine}</Text> : null}
              {!a.fine && a.fine_suppressed_reason ? (
                <Text>（精细建议未给出：{a.fine_suppressed_reason}）</Text>
              ) : null}
            </Stack>
          ))}
        </Stack>
      </Card>
    </Page>
  )
}