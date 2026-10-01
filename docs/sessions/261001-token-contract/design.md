# DESIGN-note：最终文件结果的文字—时间自洽契约

## 目标
下游用服务端最终tokens/timestamps即可直接构建字幕，正文原样拼接等于text_accu；最终文件结果不自洽时显式失败，不让下游猜清洗/对齐规则。
用户已在2026-10-01授权“行，拆卡落地根治吧”；本文件固化已批准的咨询后范围，不引入新目标。

## 非目标
不新增token_spans、词尾时间、句子切分、能力字段，不提高时间声学精度；不统一text/text_accu语义，不改SDK TXT默认输出，不对旧服务器结果新增强拒收；不改模型、HTTP存储、部署或全仓吞错治理。

## 为什么不是分区 / 删除 / 约定
- 分区：明确text为普通回显稿、tokens为字幕权威数据，消除跨稿索引。不同模型粒度允许不同，不构造固定风格承诺。
- 删除：删掉“最终短尾直接成功返回”这条收尾旁路；短音频仍不做无意义推理，但有既有session时必须正常最终格式化/同步。
- 约定：文档说明参照系是必要的，但只靠约定不能阻止生产端数据不一致；用最小长度/拼接检查把错误明确抛出，复用现有inference_failed通道，不加状态/重试/fallback。

## 方案要点与已否决方案
- tokens/timestamps是一一配对的字幕数据；text_accu定义为最终token原样拼接的正文。保留已有formatter与sync；检查不同即失败，不能用覆盖text_accu的方式掩盖同步错误。
- 检查放在来源仍在的位置：模型或对齐器token/time长度不等必须在zip/合并前拒绝；token_sync若消费不等長raw输入也须拒绝，不能先截断再检查。
- 文件final不论正常推理、短尾、空尾/EOF必须满足同一契约；合法空识别输出tokens=[]/timestamps=[]/text_accu=''仍成功。
- 不等长与拼接错误沿已有失败链发error，不得发is_final=true成功结果。WS真网络消息/子进程harness测试确认，无需模型资产或生产连接。
- 已否决：为text新增模糊映射spans；将失败text_accu静默覆盖为join；统一普通稿输出；依赖所有模型固定字/词粒度；自动重试/兼容fallback；擅自变更旧SDK接收域。
- HTTP持久入口在PR38进行中，本卡不修改其配置/存储/生命周期。共享TaskPipeline检查应自然在HTTP消费前执行；不声称未建/未测的完整HTTP生产路径验收。

## 关键不变式（本节为待锁定规格，不冒充已完成实测）
1. [规格] 最终文件结果len(tokens)==len(timestamps)，join(tokens)==text_accu。实现检查TaskPipeline最终收尾；测试tests/test_pipeline_final_contract.py+tests/test_file_result_contract_e2e.py实际发包。
2. [规格] 空/短最终片段不能绕过已有session最终格式化与检查，且不对短片段新增推理。测试空尾、1599samples短尾、1600samples正常边界、普通final。
3. [规格] 上游token/time长度不等在有损变换前fail-fast；测试模型原生timestamps和外挂aligner两种入口、sync输入长度不等。
4. [规格] 合法空结果不误拒；mic无token均分路径保持既有行为，非final行为不借机统一。
5. [规格] 不自洽的文件结果在真实WS入口收到error而非成功final；共享owner_kind='http'在已有直接pipeline测试证明异常发生于sink前，不新建HTTP服务。
6. [规格] 文档说明text不能按字符编号查tokens时间，时间按起点锚定且标点/改写继承邻近时间、不保证词尾/固定粒度。protocol_version保持2，SDK现有行为不变。

## 待验证前提
1. [源码观测，待基线复现] pipeline.py samples=None提前返回，audio.py少于1600samples返回None；core/server/segmenter.py支持短/空最终余量，seg_overlap合法0。执行器在最新主干先跑红测试确认具体错误，不以咨询推断代替复现。
2. [源码推导，待测试] sync正常非空/等长输入能构造formatted正文等式；测试插入/替换/删除/中英空格/跨token ITN/@@边界/原样输入/空字符串，支持空格式化结果但不自发扩大文本算法重构。
3. [未知] 原生模型真实声学时间精度、两份稿质量、sync重切粒度对真实字幕的影响，本卡不据此调算法。

## 验收路径
1. 执行器按TDD先写断言型红测试，在原base确认失败，再修复；红验不得是导入/语法错误。
2. 真实本机WS客户端发送PCM，真实TaskPipeline子进程用可编程假模型，捕获实际JSON最终payload，断言等式及错误不冒充成功；保留producer fixture。
3. Python3.12、websockets==15.0.1和当前websockets均跑CI同款tests全量，至少一次在无PI/DELEGATE会话身份的裸shell中跑。
4. 独立审查以冻结base..head、规格和发包证据为输入；本地漏斗通过后标PRready，确认真实CI成功，合并主干。不部署生产。
