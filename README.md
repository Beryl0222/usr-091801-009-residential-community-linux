# 旅居社区长期协约

面向云南、贵州村落旅居（两周至一年的老人与数字游民）的后端服务。管理院落/房间、
公共资源、长期协约与跨村换住，重点解决长期协约的**防超卖、版本化结算、限时授权、
崩溃恢复**，而非一晚房态。

## 领域不变量

- **权益不超卖**：同一房间同一天至多一份生效权益；整院预约覆盖院内全部房间；
  会员换住另有院落级名额上限。日期段一律半开 `[start, end)`，相邻租期可无缝衔接。
- **协约 ≠ 入住预约**：协约确定租金、押金与冻结的规则版本；预约确定某几天对
  具体房间的占用。临时离村、跨村换住、取消只改预约层，**从不删除原协约**。
- **临时离村**：房间权益保留（别人不能订），但人标记为 `temporarily_away`；
  维修封闭在离村期间仍与权益冲突，只有换住释放的时段可封闭。
- **跨村换住**：双方各自提交换住单，`accept` 时原子配对达成，容量校验互相排除
  双方将释放的原房间——两村同时办理不会出现“互为前提、谁都换不成”。
- **规则版本不可变、不追溯**：规则只能新增版本（押金、会员费、服务工时、
  三方分成比例），协约在确认日冻结当时生效版本；每月封账永远读冻结版本，
  新政策不改变老合同的任何一笔分成。
- **封账需三方确认**：村民/运营方/旅居者逐协约、逐月确认后才能封账；已封账期
  不能直接改写，纠错须显式重开（留审计事件）后重算。
- **公共资源**：菜地、书屋、快递代收等按容量登记，每条占用必须写理由；邻里提案
  与容量有限的共享活动单独报名，名额满即拒。
- **材料限时授权**：身份/健康材料只登记元数据（服务端不存内容），授权按人员、
  按材料发放，带 TTL，到期实时失效（自动收回），可提前吊销；每次查看/拒绝留痕。

## 持久化与恢复

每条写命令成功后原子落盘（临时文件 + `fsync` + `os.replace`）。进程被 `SIGKILL`
后重启即从最近完整快照恢复；若快照被外力损坏，服务**拒绝启动**，不会用空状态
覆盖旧账。

## 运行

```bash
python3 service.py --check                 # 基础配置与状态文件检查
python3 service.py --port 8000 --data x.json   # 启动 HTTP 服务
npm test                                   # 全部 27 个测试
```

## HTTP API

命令一律 `POST /api/<资源>`（动作走 `/api/<资源>/<动作>`），查询一律
`GET /api/query?kind=...`。业务冲突返回 `409 {"error": "..."}`。

| 端点 | 说明 |
| --- | --- |
| `POST /api/villages` `/courtyards` `/rooms` `/resources` | 村落、院落（含会员名额）、房间、公共资源 |
| `POST /api/rules` | 登记规则版本（不可覆盖同版本） |
| `POST /api/agreements` | 三方确认协约，冻结规则版本与押金 |
| `POST /api/agreements/cancel` | 提前退租 |
| `POST /api/bookings` `/bookings/cancel` | 入住预约（房间/整院/会员） |
| `POST /api/absences` | 临时离村（权益保留） |
| `POST /api/swaps` `/swaps/accept` `/swaps/cancel` | 换住提议、原子配对接受、取消 |
| `POST /api/closures` | 院落维修封闭（与权益冲突则拒） |
| `POST /api/resource-bookings` | 公共资源占用（必填理由，受容量限制） |
| `POST /api/proposals` `/proposals/vote` `/proposals/close` | 邻里提案 |
| `POST /api/activities` `/activities/signup` `/activities/cancel-signup` | 共享活动 |
| `POST /api/documents` `/grants` `/grants/revoke` | 材料登记、限时授权、吊销 |
| `POST /api/settlements` | 某协约某月某方确认 |
| `POST /api/periods/close` `/periods/reopen` | 封账 / 纠错重开 |
| `GET /api/query?kind=usage&village_id&day` | 某日全村房间与公共资源占用（含理由、在村/离村） |
| `GET /api/query?kind=rights&person_id&day` | 某人某日的使用权（在村/离村/换出） |
| `GET /api/query?kind=period&village_id&period` | 读取封账结果（每笔分成带规则版本） |
| `GET /api/query?kind=view-document&document_id&grantee_id&now` | 凭有效限时授权查看材料元数据 |
| `GET /api/query?kind=grants` | 授权清单（EXPIRED 状态实时计算） |
| `GET /health` | 健康探针 |

## 验收场景

`test_acceptance.py` 用真实子进程端到端覆盖：两个村同时配对换住、书屋容量冲突、
8/9/10 跨月封账、材料授权到期、随后 `SIGKILL` 突然停机；重启后核对某天的使用权
归属、在村/离村状态、公共资源占用理由、以及每笔分成采用的规则版本。另含
20 线程/10 HTTP 并发抢同一晚仅一成、损坏快照拒绝启动两项保障测试。
