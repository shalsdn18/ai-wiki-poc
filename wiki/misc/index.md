---
{
  "category": "misc",
  "entries": [
    {
      "categories": [
        "misc"
      ],
      "importance": 4,
      "input_filename": "임베디드 HMI 및 제어 GUI 설계 지침.md",
      "key_facts": [
        "물리적 어포던스(Skeuomorphic GUI)를 적용하여 사용자 오작동율을 최소화함",
        "LVGL 위젯 사용 시 50ms 이내의 시각-물리 제어 루프 동기화 권장",
        "긴급 정지 및 위험 트리거에는 오렌지색(#FF5A00)을 사용하여 시각적 위계 설정",
        "중요 제어 명령은 Long-Press 또는 2단계 확인 시퀀스를 의무화하여 오입력 차단",
        "소프트웨어 FSM 상태에 따라 유효하지 않은 제어 버튼을 비활성화하여 화면 복잡도 제어"
      ],
      "primary_category": "misc",
      "processed_at": "2026-09-07T01:37:42.322412+00:00",
      "related_topics": [
        "UI/UX 디자인 원칙",
        "임베디드 시스템 제어",
        "LVGL 그래픽 라이브러리",
        "인간-기계 상호작용(HMI)",
        "오작동 방지 설계"
      ],
      "relative_path": "임베디드 HMI 및 제어 GUI 설계 지침.md",
      "source_file": "C:\\Users\\netvision\\Desktop\\옵시디언\\MyBrain\\임베디드 HMI 및 제어 GUI 설계 지침.md",
      "source_id": "obsidian-40e0667c439c60ab60e2",
      "source_url": null,
      "summary": "임베디드 시스템의 HMI 및 GUI 설계 시 디터 람스의 어포던스 원칙을 적용하여 인지 부하를 줄이고 오작동을 방지하는 가이드라인입니다. LVGL 기반의 물리적 조작감 구현, 색상 위계 규칙, 그리고 정보 밀도 최적화를 위한 FSM 연동 전략을 다룹니다.",
      "tags": [
        "Embedded",
        "HMI",
        "UI-UX",
        "LVGL",
        "SystemDesign",
        "Affordance"
      ],
      "title": "임베디드 HMI 및 제어 GUI 설계 지침",
      "topic": "임베디드 HMI 어포던스 설계"
    },
    {
      "categories": [
        "misc"
      ],
      "importance": 3,
      "input_filename": "스마트폰 기반 엣지 서버 구축 사례 분석.md",
      "key_facts": [
        "스마트폰 기반 서버는 모바일 AP 구조상 지속적인 부하 처리에 부적합하며 스로틀링이 발생함.",
        "chroot 방식을 통해 안드로이드 환경에서 리눅스 배포판을 구동할 수 있으나 보안 격리 수준이 낮음.",
        "상시 전원 연결 시 배터리 스웰링 및 화재 위험이 존재하여 24/7 서버 운영에 치명적임.",
        "실용적인 홈랩 구축을 위해서는 확장성과 안정성이 검증된 중고 미니 PC(USFF)가 권장됨."
      ],
      "primary_category": "misc",
      "processed_at": "2026-09-07T01:37:34.407869+00:00",
      "related_topics": [
        "셀프 호스팅",
        "홈랩",
        "시스템 아키텍처",
        "임베디드 시스템"
      ],
      "relative_path": "스마트폰 기반 엣지 서버 구축 사례 분석.md",
      "source_file": "C:\\Users\\netvision\\Desktop\\옵시디언\\MyBrain\\스마트폰 기반 엣지 서버 구축 사례 분석.md",
      "source_id": "obsidian-d5ba370fc3fa414228fa",
      "source_url": null,
      "summary": "유휴 스마트폰을 활용하여 ARM 기반 엣지 서버를 구축하는 기술적 실험(PoC) 사례를 분석한 문서입니다. Termux와 chroot를 이용한 안드로이드 환경에서의 서버 운영 가능성을 검토하며, 하드웨어 제약과 화재 위험 등 실무적 한계를 지적하고 홈랩 구축을 위한 최적의 대안으로 중고 미니 PC를 제안합니다.",
      "tags": [
        "엣지컴퓨팅",
        "셀프호스팅",
        "PoC",
        "스마트폰",
        "시스템설계"
      ],
      "title": "스마트폰 기반 엣지 서버 구축 사례 분석",
      "topic": "엣지 컴퓨팅"
    },
    {
      "categories": [
        "misc"
      ],
      "importance": 3,
      "input_filename": "UI와 인간 중심 디자인.md",
      "key_facts": [
        "UI는 버튼, 레이아웃, 컬러 등 시각적 접점을 설계하는 역할을 합니다.",
        "UX는 사용자의 니즈와 감정을 해결하는 종합적인 설계 방향을 제시합니다.",
        "UI는 UX 목표를 달성하기 위한 필수적인 하위 요소입니다.",
        "UI의 완성도가 곧바로 전체 UX의 우수성을 보장하지는 않습니다."
      ],
      "primary_category": "misc",
      "processed_at": "2026-09-07T01:37:20.463662+00:00",
      "related_topics": [
        "디자인 시스템",
        "프론트엔드 개발",
        "인간 중심 디자인",
        "사용자 경험 설계"
      ],
      "relative_path": "UI와 인간 중심 디자인.md",
      "source_file": "C:\\Users\\netvision\\Desktop\\옵시디언\\MyBrain\\UI와 인간 중심 디자인.md",
      "source_id": "obsidian-7738c89a1e3a40dbe596",
      "source_url": null,
      "summary": "UI와 UX의 정의, 역할, 그리고 상호 관계를 비교 분석한 문서입니다. UX는 사용자의 전체적인 경험과 가치를 다루는 상위 개념이며, UI는 이를 시각적·물리적으로 구현하는 하위 수단임을 설명합니다.",
      "tags": [
        "design/ux",
        "ui",
        "development"
      ],
      "title": "UI와 인간 중심 디자인(UX)의 개념과 관계",
      "topic": "UI와 UX의 개념적 차이와 상호 관계"
    },
    {
      "categories": [
        "misc"
      ],
      "importance": 3,
      "input_filename": "Rust 기반 Windows 네이티브 압축 툴.md",
      "key_facts": [
        "Built using Rust for core logic and C# WinUI for the interface.",
        "Supports common formats like ZIP, 7z, RAR, and TAR.",
        "Focuses on ad-free, background-oriented compression and extraction.",
        "Licensed under GPL-3.0.",
        "Currently experiencing minor cold-start latency issues with MSIX shell extensions."
      ],
      "primary_category": "misc",
      "processed_at": "2026-09-07T01:37:13.360040+00:00",
      "related_topics": [
        "Rust programming",
        "Windows Shell Extension",
        "File compression algorithms",
        "WinUI development"
      ],
      "relative_path": "Rust 기반 Windows 네이티브 압축 툴.md",
      "source_file": "C:\\Users\\netvision\\Desktop\\옵시디언\\MyBrain\\Rust 기반 Windows 네이티브 압축 툴.md",
      "source_id": "obsidian-3b714873dfcabe7fe475",
      "source_url": null,
      "summary": "OtterZip is a new open-source, ad-free file compression tool for Windows built with Rust and WinUI, designed for minimal user intervention and seamless integration with Windows Explorer.",
      "tags": [
        "Tool/Compression",
        "OS/Windows",
        "OpenSource",
        "Rust",
        "WinUI"
      ],
      "title": "OtterZip",
      "topic": "Open-source Windows compression software"
    },
    {
      "categories": [
        "misc"
      ],
      "importance": 3,
      "input_filename": "Ruby on Rails 하드 포크 프로젝트.md",
      "key_facts": [
        "Amiko는 Ruby on Rails의 핵심 모듈을 포크하여 커뮤니티 주도로 운영되는 대안 프레임워크입니다.",
        "주요 원인은 DHH의 리더십과 정치적 세계관에 대한 커뮤니티의 반발입니다.",
        "프로젝트의 핵심 목표는 Rails 8.x와의 높은 호환성을 유지하며 안정적인 유지보수를 제공하는 것입니다.",
        "현재 초기 단계이며 프로덕션 환경 도입보다는 거버넌스 리스크를 고려하는 조직의 관찰 대상으로 적합합니다."
      ],
      "primary_category": "misc",
      "processed_at": "2026-09-07T01:37:06.726411+00:00",
      "related_topics": [
        "Ruby on Rails",
        "오픈소스 거버넌스",
        "소프트웨어 유지보수",
        "기술 부채",
        "커뮤니티 주도 개발"
      ],
      "relative_path": "Ruby on Rails 하드 포크 프로젝트.md",
      "source_file": "C:\\Users\\netvision\\Desktop\\옵시디언\\MyBrain\\Ruby on Rails 하드 포크 프로젝트.md",
      "source_id": "obsidian-1203beb6aabb25a6d091",
      "source_url": null,
      "summary": "Ruby on Rails의 창시자 DHH의 독단적 거버넌스에 반발하여 커뮤니티 주도로 시작된 하드 포크 프로젝트 'Amiko'에 대한 분석입니다. 신기능 추가보다는 Rails 8.x와의 호환성을 유지하며 안정적인 LTS를 제공하는 것을 목표로 합니다.",
      "tags": [
        "RubyOnRails",
        "Amiko",
        "Fork",
        "WebFramework",
        "Governance"
      ],
      "title": "Amiko: Ruby on Rails 하드 포크 프로젝트",
      "topic": "오픈소스 거버넌스 및 프레임워크 포크"
    }
  ]
}
---

# Misc Knowledge Wiki

## 임베디드 HMI 및 제어 GUI 설계 지침

임베디드 시스템의 HMI 및 GUI 설계 시 디터 람스의 어포던스 원칙을 적용하여 인지 부하를 줄이고 오작동을 방지하는 가이드라인입니다. LVGL 기반의 물리적 조작감 구현, 색상 위계 규칙, 그리고 정보 밀도 최적화를 위한 FSM 연동 전략을 다룹니다.

- Importance: 4/5
- Topic: 임베디드 HMI 어포던스 설계
- Related topics: UI/UX 디자인 원칙, 임베디드 시스템 제어, LVGL 그래픽 라이브러리, 인간-기계 상호작용(HMI), 오작동 방지 설계
- Categories: misc
- Tags: `Embedded`, `HMI`, `UI-UX`, `LVGL`, `SystemDesign`, `Affordance`
- Source: Local inbox document
- Input file: `임베디드 HMI 및 제어 GUI 설계 지침.md`
- Processed at: 2026-09-07T01:37:42.322412+00:00

### Key Facts

- 물리적 어포던스(Skeuomorphic GUI)를 적용하여 사용자 오작동율을 최소화함
- LVGL 위젯 사용 시 50ms 이내의 시각-물리 제어 루프 동기화 권장
- 긴급 정지 및 위험 트리거에는 오렌지색(#FF5A00)을 사용하여 시각적 위계 설정
- 중요 제어 명령은 Long-Press 또는 2단계 확인 시퀀스를 의무화하여 오입력 차단
- 소프트웨어 FSM 상태에 따라 유효하지 않은 제어 버튼을 비활성화하여 화면 복잡도 제어

## 스마트폰 기반 엣지 서버 구축 사례 분석

유휴 스마트폰을 활용하여 ARM 기반 엣지 서버를 구축하는 기술적 실험(PoC) 사례를 분석한 문서입니다. Termux와 chroot를 이용한 안드로이드 환경에서의 서버 운영 가능성을 검토하며, 하드웨어 제약과 화재 위험 등 실무적 한계를 지적하고 홈랩 구축을 위한 최적의 대안으로 중고 미니 PC를 제안합니다.

- Importance: 3/5
- Topic: 엣지 컴퓨팅
- Related topics: 셀프 호스팅, 홈랩, 시스템 아키텍처, 임베디드 시스템
- Categories: misc
- Tags: `엣지컴퓨팅`, `셀프호스팅`, `PoC`, `스마트폰`, `시스템설계`
- Source: Local inbox document
- Input file: `스마트폰 기반 엣지 서버 구축 사례 분석.md`
- Processed at: 2026-09-07T01:37:34.407869+00:00

### Key Facts

- 스마트폰 기반 서버는 모바일 AP 구조상 지속적인 부하 처리에 부적합하며 스로틀링이 발생함.
- chroot 방식을 통해 안드로이드 환경에서 리눅스 배포판을 구동할 수 있으나 보안 격리 수준이 낮음.
- 상시 전원 연결 시 배터리 스웰링 및 화재 위험이 존재하여 24/7 서버 운영에 치명적임.
- 실용적인 홈랩 구축을 위해서는 확장성과 안정성이 검증된 중고 미니 PC(USFF)가 권장됨.

## UI와 인간 중심 디자인(UX)의 개념과 관계

UI와 UX의 정의, 역할, 그리고 상호 관계를 비교 분석한 문서입니다. UX는 사용자의 전체적인 경험과 가치를 다루는 상위 개념이며, UI는 이를 시각적·물리적으로 구현하는 하위 수단임을 설명합니다.

- Importance: 3/5
- Topic: UI와 UX의 개념적 차이와 상호 관계
- Related topics: 디자인 시스템, 프론트엔드 개발, 인간 중심 디자인, 사용자 경험 설계
- Categories: misc
- Tags: `design/ux`, `ui`, `development`
- Source: Local inbox document
- Input file: `UI와 인간 중심 디자인.md`
- Processed at: 2026-09-07T01:37:20.463662+00:00

### Key Facts

- UI는 버튼, 레이아웃, 컬러 등 시각적 접점을 설계하는 역할을 합니다.
- UX는 사용자의 니즈와 감정을 해결하는 종합적인 설계 방향을 제시합니다.
- UI는 UX 목표를 달성하기 위한 필수적인 하위 요소입니다.
- UI의 완성도가 곧바로 전체 UX의 우수성을 보장하지는 않습니다.

## OtterZip

OtterZip is a new open-source, ad-free file compression tool for Windows built with Rust and WinUI, designed for minimal user intervention and seamless integration with Windows Explorer.

- Importance: 3/5
- Topic: Open-source Windows compression software
- Related topics: Rust programming, Windows Shell Extension, File compression algorithms, WinUI development
- Categories: misc
- Tags: `Tool/Compression`, `OS/Windows`, `OpenSource`, `Rust`, `WinUI`
- Source: Local inbox document
- Input file: `Rust 기반 Windows 네이티브 압축 툴.md`
- Processed at: 2026-09-07T01:37:13.360040+00:00

### Key Facts

- Built using Rust for core logic and C# WinUI for the interface.
- Supports common formats like ZIP, 7z, RAR, and TAR.
- Focuses on ad-free, background-oriented compression and extraction.
- Licensed under GPL-3.0.
- Currently experiencing minor cold-start latency issues with MSIX shell extensions.

## Amiko: Ruby on Rails 하드 포크 프로젝트

Ruby on Rails의 창시자 DHH의 독단적 거버넌스에 반발하여 커뮤니티 주도로 시작된 하드 포크 프로젝트 'Amiko'에 대한 분석입니다. 신기능 추가보다는 Rails 8.x와의 호환성을 유지하며 안정적인 LTS를 제공하는 것을 목표로 합니다.

- Importance: 3/5
- Topic: 오픈소스 거버넌스 및 프레임워크 포크
- Related topics: Ruby on Rails, 오픈소스 거버넌스, 소프트웨어 유지보수, 기술 부채, 커뮤니티 주도 개발
- Categories: misc
- Tags: `RubyOnRails`, `Amiko`, `Fork`, `WebFramework`, `Governance`
- Source: Local inbox document
- Input file: `Ruby on Rails 하드 포크 프로젝트.md`
- Processed at: 2026-09-07T01:37:06.726411+00:00

### Key Facts

- Amiko는 Ruby on Rails의 핵심 모듈을 포크하여 커뮤니티 주도로 운영되는 대안 프레임워크입니다.
- 주요 원인은 DHH의 리더십과 정치적 세계관에 대한 커뮤니티의 반발입니다.
- 프로젝트의 핵심 목표는 Rails 8.x와의 높은 호환성을 유지하며 안정적인 유지보수를 제공하는 것입니다.
- 현재 초기 단계이며 프로덕션 환경 도입보다는 거버넌스 리스크를 고려하는 조직의 관찰 대상으로 적합합니다.
