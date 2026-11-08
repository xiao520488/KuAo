"""Pydantic 数据模型定义。

包含：
  * Competition        —— 竞赛信息（列表）返回结构
  * CompetitionDetail  —— 竞赛详情返回结构（继承 Competition）
  * CategoryList       —— 分类列表返回结构

字段与 schema_v2.sql 中 competitions 表对齐。
注意：技术栈（tech_stack）在数据库中以 TEXT 存储（JSON 数组字符串），
这里对外统一暴露为 list[str]，屏蔽底层存储细节。
参赛全过程攻略（full_process_guide）在库中为 TEXT（Markdown）：
**只在详情接口返回**（列表接口不带长文，避免响应体积膨胀），
未生成时为空串；生成状态由 guide_status 表示（idle/queued/running/ready/failed）。
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class Competition(BaseModel):
    """竞赛信息返回模型（列表项）。"""

    id: int = Field(..., description="竞赛 ID")
    name: str = Field(..., description="竞赛名称")
    category: Optional[str] = Field(None, description="竞赛类别，如 算法/设计/数学建模")
    level: Literal["national", "provincial"] = Field(
        "national", description="级别：national=国家级，provincial=省级"
    )
    level_label: Optional[str] = Field(None, description="级别中文标签，如 国家级/省级")
    reg_start_month: Optional[int] = Field(
        None, ge=1, le=12, description="报名开始月份（1-12）"
    )
    reg_end_month: Optional[int] = Field(
        None, ge=1, le=12, description="报名结束月份（1-12）"
    )
    reg_cross_year: bool = Field(
        False, description="报名是否跨年（如 11 月开始、次年 3 月结束）"
    )
    contest_start_month: Optional[int] = Field(
        None, ge=1, le=12, description="比赛开始月份（1-12）"
    )
    contest_end_month: Optional[int] = Field(
        None, ge=1, le=12, description="比赛结束月份（1-12）"
    )
    official_url: Optional[str] = Field(None, description="竞赛官网地址")
    image: Optional[str] = Field(
        None, description="竞赛封面图 URL（对应数据库 image_url，只存链接不下载）"
    )
    intro: Optional[str] = Field(None, description="竞赛简介")
    summary: Optional[str] = Field(None, description="竞赛摘要")
    tech_stack: List[str] = Field(
        default_factory=list, description="涉及技术栈列表"
    )
    sort_order: Optional[int] = Field(None, description="排序权重")
    created_at: Optional[str] = Field(None, description="创建时间（本地时间字符串）")

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": 1,
                "name": "全国大学生数学建模竞赛",
                "category": "数学建模",
                "level": "national",
                "level_label": "国家级",
                "reg_start_month": 3,
                "reg_end_month": 9,
                "reg_cross_year": False,
                "official_url": "https://www.mcm.edu.cn",
                "intro": "全国大学生数学建模竞赛创办于1992年，每年一届。",
                "summary": "国内影响力最大的基础性学科竞赛之一。",
                "tech_stack": ["Python", "MATLAB", "LaTeX"],
                "sort_order": 1,
                "created_at": "2025-01-01 00:00:00",
            }
        }
    }


class CompetitionDetail(Competition):
    """竞赛详情返回模型。

    详情页包含列表的全部字段（含 summary 摘要与 full_process_guide 攻略），
    直接继承 Competition，语义上表示单条完整记录。

    为什么 full_process_guide 只放在详情：
        攻略是 2-5KB 的 Markdown，列表接口一次返回几十条会平白多出上百 KB；
        前端列表页也不使用该字段（只有详情页渲染）。
    """

    model_config = Competition.model_config

    full_process_guide: str = Field(
        "",
        description=(
            "参赛全过程攻略（Markdown）：赛前准备、报名与材料、比赛形式、"
            "展示与答辩、常见坑、官方核对清单。未生成时为空串。"
        ),
    )
    guide_status: str = Field(
        "idle",
        description="攻略生成状态：idle / queued / running / ready / failed",
    )
    guide_generated_at: Optional[str] = Field(
        None, description="攻略生成时间（本地时间字符串），未生成时为空"
    )


class CategoryList(BaseModel):
    """分类列表返回模型。"""

    categories: List[str] = Field(
        default_factory=list, description="去重后的竞赛分类名称列表"
    )
    total: int = Field(0, description="分类数量")

    model_config = {
        "json_schema_extra": {
            "example": {
                "categories": ["人工智能", "信息安全", "数学建模"],
                "total": 3,
            }
        }
    }

# ---------------------------------------------------------------------------
# 获奖作品（awards）
# ---------------------------------------------------------------------------
class Award(BaseModel):
    """获奖作品返回模型。

    字段与 awards 表对齐；tech_stack 在库中为 JSON 字符串，
    对外统一暴露为 list[str]。
    """

    id: int = Field(..., description="作品 ID")
    title: Optional[str] = Field(None, description="作品标题")
    summary: Optional[str] = Field(None, description="作品简介")
    highlights: Optional[str] = Field(None, description="作品亮点")
    team: Optional[str] = Field(None, description="团队/作者")
    track: Optional[str] = Field(None, description="赛道/组别")
    year: Optional[int] = Field(None, description="获奖年份")
    session: Optional[str] = Field(None, description="赛段，如 国赛/省赛")
    award_grade: Optional[str] = Field(None, description="获奖等级，如 一等奖")
    level: Optional[str] = Field(None, description="赛事级别")
    tech_stack: List[str] = Field(
        default_factory=list, description="技术栈（已解析为列表）"
    )
    origin_url: Optional[str] = Field(
        None,
        description="作品原文链接（数据库 source_url 字段，前端用于「查看原文」跳转）",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": 101,
                "title": "果蔬采摘双臂协作机器人",
                "summary": "面向设施农业的双臂协作采摘机器人。",
                "highlights": "双臂协同调度算法",
                "team": "机器人队",
                "track": "机器人创意",
                "year": 2024,
                "session": "国赛",
                "award_grade": "一等奖",
                "level": "national",
                "tech_stack": ["Python", "ROS", "C++"],
                "origin_url": "https://github.com/example/project",
            }
        }
    }


class AwardList(BaseModel):
    """获奖作品列表返回模型。"""

    competition_id: int = Field(..., description="竞赛 ID")
    total: int = Field(0, description="作品数量")
    items: List[Award] = Field(default_factory=list, description="获奖作品列表")
