// 所有课程页共用的小组件。新页面直接 <script src="../assets/course.js"></script>，不要复制代码。

// 1. 打印时展开所有参考答案，打完再收起来
(function () {
  let opened = [];
  window.addEventListener("beforeprint", function () {
    opened = [];
    document.querySelectorAll("details:not([open])").forEach(function (d) {
      d.open = true;
      opened.push(d);
    });
  });
  window.addEventListener("afterprint", function () {
    opened.forEach(function (d) { d.open = false; });
  });
})();

// 2. 选择题：每题点一次就判对错，并显示解释
//
// 用法：
//   <div data-quiz="my-quiz"></div>
//   <script type="application/json" id="my-quiz">
//     [{"prompt": "...", "options": ["...", "..."], "answer": 0, "explain": "..."}]
//   </script>
// answer 是正确选项的下标。各选项尽量一样长，不要用长短泄露答案。
(function () {
  function render(root) {
    const data = JSON.parse(document.getElementById(root.dataset.quiz).textContent);
    let answered = 0;
    let correct = 0;

    const score = document.createElement("p");
    score.className = "quiz-score";
    score.textContent = "共 " + data.length + " 题，每题只能选一次。";

    data.forEach(function (q, qi) {
      const box = document.createElement("div");
      box.className = "quiz-q";

      const prompt = document.createElement("div");
      prompt.className = "prompt";
      prompt.textContent = (qi + 1) + ". " + q.prompt;
      box.appendChild(prompt);

      const options = document.createElement("div");
      options.className = "options";
      const buttons = q.options.map(function (text, oi) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "option";
        b.textContent = String.fromCharCode(65 + oi) + ". " + text;
        b.addEventListener("click", function () {
          buttons.forEach(function (other) { other.disabled = true; });
          buttons[q.answer].classList.add("correct");
          if (oi !== q.answer) b.classList.add("wrong");
          box.classList.add("answered");
          answered += 1;
          if (oi === q.answer) correct += 1;
          feedback.textContent = (oi === q.answer ? "答对了。" : "正确答案是 " + String.fromCharCode(65 + q.answer) + "。") + q.explain;
          score.textContent = answered < data.length
            ? "已答 " + answered + " / " + data.length + "，答对 " + correct + " 题。"
            : "全部答完：" + data.length + " 题答对 " + correct + " 题。答错的题，回参考文档对应小节再看一遍。";
        });
        options.appendChild(b);
        return b;
      });
      box.appendChild(options);

      const feedback = document.createElement("div");
      feedback.className = "feedback";
      box.appendChild(feedback);

      root.appendChild(box);
    });
    root.appendChild(score);
  }

  function init() {
    document.querySelectorAll("[data-quiz]").forEach(render);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
