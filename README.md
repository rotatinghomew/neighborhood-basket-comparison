# 邻里商圈价格篮子的缺货与替代

这里保存邻里商圈价格篮子的缺货与替代工作所用的匿名合成记录。文件不包含分析结果或居民个人资料。

## 资料

`samples/` 中每个文件都是独立的 UTF-8 JSON 文档：`quotes`、`shops`、`needs`、`discounts`、`households`、`corrections`、`publication`。日期是当地日历日期。空值表示没有对应观测或状态资料，不是数值零。

price_cents 为报价单位的分值，grams 是单位克重，step 是一次购买的单位倍数。报价按同周同店同时段适用。折扣的 units 是最低整单购买单位数；maximum 是折扣可用次数。会员条件由家庭资料列明。

## 运行

使用 Python 3.11，无需第三方依赖或服务。`python3 inspect_samples.py` 逐个读取资料并列出记录数；`python3 -m unittest -v` 检查关键原始记录间的关系。输出只描述原始资料，不是业务比较结果。
