# BioMars: 25 个设计方向

以「看见一个实验入口，输入问题，开始」为共同使用路径。每款只使用品牌、短标题和输入操作，具体说明留在原 Studio 工作区。展示页提供总览、风格分类、全屏、左右方向键、收藏及双款对比。收藏保存在本机浏览器。输入内容只暂存在会话中，然后交给 Studio，展示页不模拟生成结果。

25 个方向的字体、配色和交互说明由 [`styles.js`](../web/directions/styles.js) 维护；各自独立构图在 [`scene.js`](../web/directions/scene.js) 与 [`scenes.css`](../web/directions/scenes.css)。主视觉与生成提示词见 [资产记录](design-assets.md)。

设计研究入口：[Apple](https://www.apple.com/) 的简短标题与视觉主导入口，[Pentagram](https://www.pentagram.com/work) 的品牌与编排作品，以及 [Awwwards Typography](https://www.awwwards.com/websites/typography/) 的字体表达案例。实现采用原创构图及生成图像，未复制其网页或品牌素材。

交互提供可见聚焦状态、键盘切换、触控按钮、减少动态效果支持；装饰动画不会触发业务请求。浏览器窗口过矮时，单款预览内部可滚动，也可进入全屏。
