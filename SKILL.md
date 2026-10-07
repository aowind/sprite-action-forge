---
name: sprite-action-forge
description: 用本地模型（ComfyUI + MiniMax H3）把一张立绘/设定图批量做成全套 2D 动作精灵表——站立、跑动、走动、攻击、技能、挨打，以及独立的攻击/技能特效精灵。当需要给游戏角色或怪物做逐帧动作、攻击动画、技能特效、精灵表（sprite sheet）、绿幕抠图抽帧、动作循环质量检测时使用。关键词：动作精灵、精灵表、sprite sheet、逐帧动画、立绘转动作、攻击动画、技能特效、绿幕抠图、本地生视频、H3。
license: MIT
---

# 动作精灵锻造（sprite-action-forge）

一张立绘 → 一整套动作精灵表。**全部本地跑**（ComfyUI + MiniMax H3），不依赖云服务。

## 什么时候用

- 有角色/怪物的立绘或设定图，要把它变成游戏里能播的动作（站立/跑动/走动/攻击/技能/挨打）
- 要做**攻击/技能特效**（刀光、落雷、冲击），而且要能和角色动画**分开调**
- 要把动作接进游戏：状态机、招式时序（动作播完才掉血）、位移、特效叠加

## 环境要求

| 依赖 | 说明 |
|---|---|
| ComfyUI | 本地服务（默认 `127.0.0.1:8188`）。`bash scripts/ensure_comfy.sh` 会自动找并补起 |
| MiniMax H3 模型 | 放 ComfyUI 的 models 目录。**具体文件名见 `scripts/comfy_client.py` 顶部常量**（UNET/CLIP/VAE/TurboLoRA）。8GB 显存用 `--lowvram` + KJNodes 的两个无损压显存节点 |
| Python | 需要 `numpy`、`Pillow`。ffmpeg 见下 |
| ffmpeg | 抽帧用。优先 `pip install imageio-ffmpeg`；也支持随包放在 `_py/` 目录；或系统 PATH；或用 `SPRITE_FFMPEG=/path/to/ffmpeg` 指定 |

## 三步走

```bash
# 0) 起服务（有则什么都不做）
bash scripts/ensure_comfy.sh

# 1) 角色动作（每个动作一条精灵表 + 一份 meta）
python scripts/sprite_forge.py \
  --art 少侠.png --char hero \
  --actions idle,run,attack,skill,hurt \
  --out ./_gen_actions \
  --style "crisp stylized cel-shaded chibi wuxia boy"

# 怪物换风格描述即可（写法见 references/prompts.md）
python scripts/sprite_forge.py --art 野猪.png --char mob_boar \
  --actions idle,run,attack,hurt --style "clean papercraft cut-out style"

# 2) 攻击/技能特效（独立素材）
python scripts/fx_forge.py --all --out ./_gen_actions/fx

# 3) 装配进 web 目录 + 生成清单（会自动备份被覆盖的原文件）
python scripts/install_sheets.py --src ./_gen_actions --dest ./public/assets/actions \
  --fx-src ./_gen_actions/fx --fx-map '{"attack":"slash","skill":"burst"}'
```

**只重跑后半段**（视频还在、只想改对齐/选窗）：加 `--reuse-video`，几秒钟一个，不花 GPU 时间。

## 配方速查

| 动作 | 时长 | 帧/帧率 | 说明 |
|---|---|---|---|
| idle | **2.0s** | 8 @6fps | 循环。呼吸/微动 |
| walk / run | **2.0s** | 10 @10-12fps | 循环，**必须原地跑**（提示词要写 treadmill） |
| attack | 1.0s | 8 @12fps | 一次性。提示词带 `no_fx` |
| skill | 1.5s | 10 @10fps | 一次性 + 取尾段选窗。提示词带 `no_fx` |
| hurt | 0.7s | 6 @12fps | 一次性 |

🔴 **循环动作一定要给够时长（≈2 秒）**：1 秒（39 帧）两端被起停包络占满、没有稳定的中段循环窗，
实测质量分 1.16（WEAK）；给到 2 秒（56 帧）后同一动作拿到 0.32（CLEAN）。

## 输出

```
_gen_actions/<char>_<action>.webp   精灵表（每帧等宽、内容不裁、跨动作人物等高）
_gen_actions/<char>_<action>.json   meta：帧数/fps/loop/content_h_ratio/baseline_ratio/选窗质量
```

**循环质量分**（meta 的 `window.score`）：`<0.5` 干净 ✅ / `0.5~0.8` 看一眼 ⚠️ / `>0.8` 步幅超出窗 ❌
（加长 `--seconds` 或重新生成）

## 五条铁律（不看会踩坑）

1. **帧几何要"算出来不裁"，不能试出来** —— 画布尺寸由内容实际伸展范围反推 + 上下对称留白。
   详见 `references/geometry.md`。症状：**人物只显示一半**。
2. **`center` 按动作区分** —— 循环类逐帧居中（消漂移）；一次性类整段平移（**保留前冲位移**，
   居中会把位移压平）。
3. **怪物必须给专属提示词** —— 通用的"站着呼吸"用在四足/纸雕怪上，模型会生成**静止插画**
   （逐帧像素几乎一样）。措辞要具体到身体部位。见 `references/prompts.md`。
4. **特效一律独立成精灵**，别烘进角色帧 —— 会把单帧宽度撑大（实测 322px vs 站立 174px），
   而且没法单独调位置/时长。
5. **比例以最终素材为准** —— 装配时从实际 webp 重量 `content_h_ratio`/`baseline_ratio`，
   别信生成时的 meta（素材会被重生成/回退）。否则人物会突然变大变小、脚悬空。

## 接进游戏

动作状态机、招式时序（**动作播完才结算伤害**）、程序化攻击位移、特效叠加的完整做法见
`references/integration.md`。三条最容易错的：

- **触发一律判上升沿**，别判电平（电平会让动画每帧被重置 → 永远停在第 0 帧）
- 出手要**挂起**，等动作时长到了再结算（否则"怪都死了技能才刚开始播"）
- 一次性动作的时长**从素材推导**（帧数/fps），别写死常量（素材换了常量没换 = 定时炸弹）

## 排查

先看 `references/pitfalls.md`（按**现象**查因）。最常见的三个：

| 现象 | 大概原因 |
|---|---|
| 人物只显示一半 / 缺一块 | 对齐时内容被裁（画布尺寸没由内容反推） |
| 人物在攻击那几帧整个不画 | 坐标算出了 `NaN`（目标 `x` 是 undefined），绘制处要挡一道 `Number.isFinite` |
| 动画卡住不动 | 触发判成了电平；或主循环某帧抛错（先看 console 的 `pageerror`） |

## 参考

- `references/pipeline.md` — 完整流水线与每一步的原理
- `references/prompts.md` — 提示词库与措辞技巧（人物/怪物/特效）
- `references/geometry.md` — 帧几何与对齐算法
- `references/integration.md` — 游戏侧接入
- `references/pitfalls.md` — 踩坑清单（现象 → 根因 → 修法）
