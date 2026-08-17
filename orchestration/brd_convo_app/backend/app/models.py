from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class StartResponse(BaseModel):
    session_id: str
    message: Dict[str, str]


class MessageRequest(BaseModel):
    session_id: str
    user_input: str


class MessageResponse(BaseModel):
    session_id: str
    message: Dict[str, str]
    phase: str
    progress: Optional[str] = None
    download_url: Optional[str] = None
    preview: Optional[str] = None


class SessionState(BaseModel):
    session_id: str
    phase: str

    # --------------------
    # Core session info
    # --------------------
    company: Optional[str] = None
    section: str = "1.1"
    messages: List[Dict[str, str]] = Field(default_factory=list)

    #added new states - 01-04
    pending_question: Optional[dict] = None
    proposed_answer: Optional[str] = None
    return_phase: Optional[str] = None
    research_cache: Dict[str, str] = Field(default_factory=dict)
    next_phase: Optional[str] = None
    # --------------------
    # Question repositories
    # --------------------
    all_intro_questions: List[Dict[str, Any]] = Field(default_factory=list)
    all_br_questions: List[Dict[str, Any]] = Field(default_factory=list)
    all_sc_questions: List[Dict[str, Any]] = Field(default_factory=list)
    all_dp_questions: List[Dict[str, Any]] = Field(default_factory=list)
    all_ip_questions: List[Dict[str, Any]] = Field(default_factory=list)
    all_di_questions: List[Dict[str, Any]] = Field(default_factory=list)
    selected_sections: List[str] = Field(default_factory=list)
    
    # --------------------
    # Active conversation flow
    # --------------------
    active_question_list: List[Dict[str, str]] = Field(default_factory=list)
    current_question_index: int = 0
    qa_records: List[Dict[str, Any]] = Field(default_factory=list)

    # --------------------
    # Section outputs ✅ (THIS FIXES YOUR ERROR)
    # --------------------
    section1_sections: Optional[Any] = None
    br_sections: Optional[Any] = None
    ssc_sections: Optional[Any] = None
    dp_sections: Optional[Any] = None
    ip_sections: Optional[Any] = None 
    di_sections: Optional[Any] = None 

    # --------------------
    # Research & generation
    # --------------------
    research_context: Optional[str] = None
    gen_messages: List[Any] = Field(default_factory=list)

    # --------------------
    # Paragraph approval flow
    # --------------------
    paras: List[tuple[int, str]] = Field(default_factory=list)
    current_para_index: int = 0
    approved_paras: Dict[int, str] = Field(default_factory=dict)
    awaiting_approval: bool = False

    # --------------------
    # Private questions
    # --------------------
    private_questions: List[tuple[str, str]] = Field(default_factory=list)
    private_answers: Dict[str, str] = Field(default_factory=dict)
    current_private_index: int = 0

    # --------------------
    # Output files
    # --------------------
    txt_path: Optional[str] = None
    docx_path: Optional[str] = None