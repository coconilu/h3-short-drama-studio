---
name: Rain Call Shadow Cut
colors:
  primary: "#071011"
  on-primary: "#F0ECE2"
  muted: "#A7AEA8"
  accent: "#E0A33A"
  danger: "#9E2330"
typography:
  headline:
    fontFamily: Noto Sans JP
    fontSize: 8.5rem
    fontWeight: 700
  label:
    fontFamily: JetBrains Mono
    fontSize: 1.1rem
    fontWeight: 600
motion:
  energy: moderate
  entrance: waterfall-entry
  transition: color-dip-to-black
  easing: power4.out
---

## Overview

概念角度：来电不是通知，而是一道把林夏重新锁进三年前的指令。整体沿用 Shadow Cut 的黑色电影结构，但把通用血红改为正片已有的雨衣红，并以便利店灯光的琥珀色承担唯一信息强调。

## Composition

- 焦点：后视镜中的林夏与后排模糊人脸，必须使用真实正片帧。
- 边缘锚点：左上角剧集编号、左侧琥珀色竖线、右下角英文小字。
- 支撑信息：片名、单集副标题、悬疑短剧标签。
- 背景处理：原帧等比铺满，只加暗色遮罩和轻微推近，不重绘、不换脸、不改变正片本身。

## Typography

- 中文主标题使用可嵌入的 `Noto Sans JP` 粗体，保持方正、克制。
- 编号与技术性小字使用 `JetBrains Mono`，制造“来电记录/案件编号”的冷感。
- 不使用装饰性书法、霓虹字、发光描边。

## Motion

- 片名字级以 `waterfall-entry` 从下方快速落定，透明度只做瞬时显隐。
- 片头进入正片、正片进入片尾均使用克制的黑场色沉，不覆盖正片内容。
- 正片母版不做缩放、调色或变速。

## Do / Don't

- Do：保留大面积负空间，让后排人脸成为第二阅读层。
- Do：使用真实画面中的红色雨衣和便利店暖光作为天然色彩线索。
- Don't：新增 BGM、故障特效、假雨滴、AI 生成封面人物或宣传文案。
