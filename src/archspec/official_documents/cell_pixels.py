"""固定单元格的有界本机裁剪；自动与按需补读共用同一坐标。"""
from math import isfinite

import cv2
import numpy as np
import pymupdf

from archspec.official_documents.runtime_identity import frozen_source_identity  # isort: skip


def cell_clip(page, box):
    if (len(box) != 4 or any(type(x) not in {int, float} or not isfinite(x) for x in box)
            or not 0 <= box[0] < box[2] <= 1 or not 0 <= box[1] < box[3] <= 1):
        raise ValueError("单元格位置无效")
    if page.rotation:
        raise ValueError("旋转原页暂不支持单元格补读，请对照原图")
    a,b,c,d=box
    clip=pymupdf.Rect(a*page.rect.width+2,b*page.rect.height+2,
                      c*page.rect.width-2,d*page.rect.height-2)
    raster=(clip*pymupdf.Matrix(6,6)).irect
    if clip.is_empty or clip.height>120 or raster.width*raster.height>1_000_000:
        raise ValueError("单元格过大，暂不能安全补读，请对照原页")
    return clip, raster.width*raster.height


def crop_cell(page, clip):
    pix=page.get_pixmap(matrix=pymupdf.Matrix(6,6),clip=clip,alpha=False)
    if pix.width*pix.height>1_000_000:
        raise ValueError("单元格超过补读像素上限")
    pixels=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,pix.n)
    gray=cv2.cvtColor(pixels,cv2.COLOR_RGB2GRAY)
    ys,xs=np.where(gray<120)
    if not len(xs):
        raise ValueError("单元格字迹无法定位，请对照原页")
    x0,y0=max(0,int(xs.min())-4),max(0,int(ys.min())-4)
    x1,y1=min(pix.width,int(xs.max())+5),min(pix.height,int(ys.max())+5)
    tight=pixels[y0:y1,x0:x1]
    ink=(gray[y0:y1,x0:x1]<120).astype(np.uint8)
    count,_,stats,centers=cv2.connectedComponentsWithStats(ink,connectivity=8)
    components=[i for i in range(1,count) if stats[i,cv2.CC_STAT_AREA]>=4]
    height=max((stats[i,cv2.CC_STAT_HEIGHT] for i in components),default=0)
    # 主笔画中心须同一基线；忽略小数点等短笔画，防两行数字拼成一个数。
    mains=[i for i in components if stats[i,cv2.CC_STAT_HEIGHT]>=max(4,height*.45)]
    single=bool(mains) and max(centers[i,1] for i in mains)-min(centers[i,1] for i in mains)<=height*.35
    pixels=cv2.copyMakeBorder(tight,24,24,24,24,cv2.BORDER_CONSTANT,value=(255,255,255))
    if pixels.shape[0]*pixels.shape[1]>1_000_000:
        raise ValueError("单元格超过补读像素上限")
    return {"pixels":pixels,"tight_pdf_box":[(pix.x+x0)/6,(pix.y+y0)/6,(pix.x+x1)/6,(pix.y+y1)/6],
            "padding_pixels":24,"single_line":single}


LOADED_SOURCE_IDENTITY = frozen_source_identity(__file__)
