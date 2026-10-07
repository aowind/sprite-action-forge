# 提示词库与措辞技巧

> 同一个模型、同一张立绘，**措辞差别能决定动作是"活的"还是"静止插画"**。
> 这一节是可以直接抄的模板 + 背后的处理规律。

---

## 一、通用骨架

每条动作提示词 = **主体描述** + **动作描述** + **镜头/背景锁**（+ 可选 `no_fx`）

```
{subj} + <动作描述> + Locked static camera, no camera movement.
Flat pure green screen background, no ground shadow, {style},
same character design as the first frame. Silent, no audio, no music, no speech.
```

`{subj}` 用**画风描述**而不是角色名（模型不认识你的角色名）：
- 人物：`crisp stylized game character art, cel-shaded chibi`
- 纸雕怪：`clean papercraft cut-out style, layered paper shapes with soft shadows`

---

## 二、循环动作：**必须写"原地"**

不写的话角色会跑出画面。固定用这个句式（`treadmill` 是关键锚）：

```
{subj} runs in place like on a treadmill, a steady repeating run cycle:
knees high, arms pumping, torso leaning forward, both feet leave the ground
between steps. Stays centered and does not travel across the frame.
```

逆效果词：`walking, standing still, turning around`

### ⚠️ 跑步/走路的躯干旋转要单独写

不写的话**身体朝向被焊死**，看着像在滑行而不是在跑：

> 说明"哪条手臂向前摆，那一侧肩膀就向前送出去、腰跟着扭"，并逐帧给出躯干朝向。
> 角色面朝画面右侧时：左肩向前 → 转向观众、看到更多正面；
> **右肩向前 → 转过去、看到后背**。

---

## 三、怪物：措辞必须**具体到身体部位**

🔴 这是最容易踩的坑。通用的「站着呼吸、微微起伏」用在**纸雕四足怪**上，
模型直接当成**静止插画** —— 生成出来的 6 帧逐像素几乎一样。

**判据**：对齐后的 `spread_bottom` 是 **0.0px**。

### 四足怪（野猪类）

| 动作 | 措辞要点 |
|---|---|
| idle | `breathing heavily: the chest and belly rise and fall, both ears twitch, the short tail flicks side to side, the head bobs up and down slightly, the front legs shift their weight` |
| run | `trotting in place on a treadmill: all four legs step in quick sequence, hooves lift clearly off the ground and come back down, the body bobs up and down with each step` |
| attack | `snorting, pawing the ground twice, then charging forward head-first with its tusks lowered and thrusting, then backing off` |
| hurt | `the whole body jolts backward, the head jerks up, the legs scrabble for balance, then settles back` |

### 人形怪（强盗/山贼类）

| 动作 | 措辞要点 |
|---|---|
| idle | `shoulders rise and fall, the weapon is held ready, weight shifts from one foot to the other, the beard and headband sway slightly` |
| walk | `a steady repeating walk cycle, steps forward one foot at a time, arms and the big cleaver swinging opposite the legs, body bobbing` |
| attack | `raising the cleaver high overhead, then chopping down hard in one fast swing, then pulling back to the ready pose` |
| hurt | `staggers back half a step, the torso twists away, the weapon arm drops, then recovers` |

> 规律：**要写出"只有真在动才会出现的事"** —— 蹄子离地、耳朵抖、尾巴摆、
> 重心在两只脚之间移动。全都是静物不可能有的现象。

---

## 四、一次性动作（攻击/技能/挨打）

- 结尾要**回到起始姿势**：`then returns to the starting ready pose`（方便接回循环动作）
- 逆效果词写清"不要复合动作"：`walking, running, standing idle, multiple strikes`
- **技能不要写大挥砍** —— 快速挥砍必然带出运动拖尾（见 `pitfalls.md`）。
  改成 `slowly raises the weapon overhead, holds it high while gathering power, then lowers it back`
  + 逆效果词 `big fast swing, spinning`，力量感交给独立特效

---

## 五、禁止画面特效（攻击/技能必加）

特效要**单独出精灵**，别烘进角色帧。但措辞不够会漏：

```
Absolutely no visual effects: no energy trail, no glowing arc, no crescent light sweep,
no light streaks, no particles, no impact flash, no dust, no smoke,
no colored haze or mist, no motion blur, no speed lines, no aura,
no glow around the weapon or the body.
The weapon is plain, clean and fully visible with a sharp simple silhouette.
The background stays pure flat solid green with nothing drawn on it at all.
```

⚠️ 提醒：**这条只能减少、不能根除**。快速挥砍的运动拖尾是模型固有行为，
实测强化措辞后反而更大。真正有效的是 `tail` 选窗或改动作设计。

---

## 六、特效素材的提示词

特效在**纯黑底**上生成（不绿幕），要点是**把该特效的物理特征全部点名**：

| 特效 | 要点 | 反例 |
|---|---|---|
| 刀光 slash | `a single bright white crescent sword slash arc, a thin curved energy blade trail sweeping from upper right to lower left, glowing white core with a warm yellow edge, sharp clean shape` | — |
| 落雷 burst | `a single lightning bolt striking straight down … one jagged zigzag streak with one or two sharp forked branches, a blinding white hot core wrapped in electric cyan glow, and at the instant of impact a flat radial shockwave ring bursting outward along the ground` | ❌ `a vertical pillar of energy` → 出来是一根模糊竖条 |

逆效果词统一：`character, person, hands, weapon, text, watermark, background scenery, fog, cloud, smoke`
（落雷再加 `dim, blurry`）

**黑底是必须的**：亮度直接当 alpha 用，不需要抠色；游戏端用 `lighter` 加法叠加。

---

## 七、写提示词时容易忽略的两个坑

1. **Python 字符串里写中文提示词时，内层别用 ASCII 双引号**
   （`"右肩在前"` 会把字符串截断 → `SyntaxError`）。要强调就加粗或用「」。
2. **要精确控制动作就给参考图**，别指望文字。实测：
   文字描述 ❌ / 文字+量化角度 ❌ / **动作框架图并排拼图** ✅。
   参考图用 **1:1**（如 1024×1024）—— 用 2:1 的话模型按输入宽高比输出，
   角色只占中间一小块、有效分辨率浪费一半。
