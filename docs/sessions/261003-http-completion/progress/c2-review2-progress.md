阶段：源码 / spec / 新增测试完整初审完成。
结论：代码以 jobs.state 与 terminal_at 筛选 DONE/FAILED 七日前源；source-presence 查实际 stat；cleanup 与 SQLite、unlink 走同一单 worker。新增整文件测试含真实 listener、HttpFileRunner 队列、持有源至 decoder.close 的周期清理、结果载荷、unlink 错误与 stop 等待；尚未验证短系统单元、真实 ffmpeg 消费和四类退出场景，暂不下最终 verdict。
关键决策与否决：冻结审查范围为 820c3a2ee4fccc1b44187bd99c40cf99c16e1ca2..5134720e058e0e9ae3d3feddebf4942f8bf7ed7a，只评 C2 两生产文件与 tests/test_http_cleanup.py；不读实现 evidence/progress/review1 产物，不跟随 HEAD，不修实现、不用替身 close 代替真实 decoder。
唯一下一步：检查 App / SocketManager / runner 的真实启动接线后，构造自有 systemd 短进程 probe，执行真实 TCP/SQLite/runner/ffmpeg 的限定场景并记录可核对状态。
