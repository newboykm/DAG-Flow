---
name: test
description: 跑测试 → 诊断失败 → 修 → 重跑直到绿（同一处连续失败 2 次就停）。识别 go/npm/pnpm/yarn/pytest/cargo。
---

# Skill: test

跑测试并修失败。运行在主循环。

## 工作方式
1. 识别测试命令：go.mod → `go test ./...`；package.json scripts.test → `npm test`；pyproject/requirements → `pytest`；Cargo.toml → `cargo test`。拿不准就问，别猜。
2. 跑起来，抓 stdout+stderr；长命令转后台用 wait。
3. 读失败：哪些测试挂了、真实报错、抛出位置（文件+行）。
4. 分类修：
   - 生产 bug（测试抓到真缺陷）→ 修生产代码；
   - 测试 bug（测试错、代码对）→ 修测试并明说；
   - 环境问题（缺依赖/工具/夹具）→ 说明并停，装包或改配置前先确认。
5. 改完重跑，迭代。
6. 停手条件：全绿 → 报告改了什么；同一行同一失败连 2 次 → 停并说清；3+ 无关失败 → 一次一个，先最小的。

## 不要
- 不得未经确认安装/升级依赖；
- 不得 skip/删/禁用失败测试强行变绿；
- 不得改测试运行器配置去掩盖失败。

## 汇报
- 每轮开头给一行状态（“▸ running pytest …”“▸ 2 failures，第一个是 …”），让用户始终知道进度。
