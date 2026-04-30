// step2glb — convert STEP to binary glTF (GLB) using OCCT.
// Preserves assembly hierarchy and per-face colors.
//
// Tessellation uses OCCT's BRepMesh_IncrementalMesh in either:
//   * absolute mode  — chord deflection in millimetres (Mayo / SimLab default)
//   * relative mode  — chord deflection as a fraction of each face's bbox
//
// Absolute mode produces uniform-looking triangles across an assembly because
// every face hits the same physical chord tolerance. Relative mode adapts
// per-face but can produce visually inconsistent triangle sizes when faces
// vary widely in size.
//
// Usage:
//   step2glb <input.step> <output.glb>
//                [--linear <v>] [--angular <rad>] [--relative] [--min-size <v>]
//
//   --linear      chord tolerance. mm in absolute mode, or fraction of each
//                 face's bbox with --relative. (default 1.0)
//   --angular     max angular deviation per triangle, radians (default 0.349 = 20°)
//   --relative    use relative chord deflection instead of absolute mm
//   --min-size    smallest allowed triangle edge (default Precision::Confusion)

#include <iostream>
#include <string>
#include <cstdlib>
#include <vector>

#include <STEPCAFControl_Reader.hxx>
#include <STEPControl_Reader.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <Interface_Static.hxx>

#include <TDocStd_Document.hxx>
#include <XCAFApp_Application.hxx>
#include <XCAFDoc_DocumentTool.hxx>
#include <XCAFDoc_ShapeTool.hxx>
#include <XCAFDoc_ColorTool.hxx>
#include <TDF_LabelSequence.hxx>
#include <TDF_Label.hxx>

#include <TopoDS.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Iterator.hxx>
#include <TopExp_Explorer.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <IMeshTools_Parameters.hxx>
#include <BRep_Tool.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepLProp_SLProps.hxx>
#include <Poly_Triangulation.hxx>
#include <TopLoc_Location.hxx>
#include <ShapeFix_Shape.hxx>
#include <TDataStd_Name.hxx>
#include <TDF_Tool.hxx>
#include <TCollection_ExtendedString.hxx>
#include <Quantity_Color.hxx>
#include <XCAFDoc_ColorType.hxx>
#include <Precision.hxx>
#include <gp.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Vec3f.hxx>

#include <RWGltf_CafWriter.hxx>
#include <TColStd_IndexedDataMapOfStringString.hxx>
#include <Message_ProgressRange.hxx>
#include <TCollection_AsciiString.hxx>

static void print_usage() {
    std::cerr
        << "Usage: step2glb <input.step> <output.glb>\n"
        << "  [--linear <v>] [--angular <rad>] [--relative] [--min-size <v>]\n"
        << "  [--no-shape-fix]          disable ShapeFix_Shape healing fallback\n"
        << "  [--no-analytic-normals]   write OCCT's tessellation normals instead of\n"
        << "                            evaluating per-node normals from NURBS surfaces\n";
}

// Count triangles produced by tessellation across every face of `shape`.
// Used to decide whether tessellation succeeded.
static int count_triangles(const TopoDS_Shape& shape) {
    int n = 0;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face& f = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(f, loc);
        if (!tri.IsNull()) n += tri->NbTriangles();
    }
    return n;
}

// Run OCCT's general ShapeFix on a shape: rebuilds wires, sets tolerance
// envelopes, fixes degenerate edges, etc. Returns the healed shape (or the
// original if Perform produces nothing useful).
static TopoDS_Shape shape_fix(const TopoDS_Shape& shape) {
    Handle(ShapeFix_Shape) fixer = new ShapeFix_Shape(shape);
    fixer->Perform();
    TopoDS_Shape healed = fixer->Shape();
    if (healed.IsNull()) return shape;
    return healed;
}

// Replace each tessellation node's normal with one evaluated analytically
// from the underlying NURBS surface. OCCT's default normals are derived from
// the mesh triangle geometry, so a coarsely tessellated cylinder shades like
// a faceted drum. The analytic normals follow the true surface curvature,
// giving smooth shading regardless of triangle count.
//
// We do NOT weld vertices across OCCT face boundaries, so each face keeps
// its own per-node normals — sharp mechanical creases stay sharp because
// adjacent faces emit separate glTF primitives with independent vertices.
static int compute_analytic_normals(const TopoDS_Shape& shape) {
    int nFaces = 0;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face& face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        if (tri.IsNull() || !tri->HasUVNodes() || tri->NbNodes() == 0) continue;

        // Step 1: establish a mesh-derived (face-averaged) baseline so EVERY
        // vertex carries a non-zero, sensibly-oriented normal even before we
        // touch it. Analytic evaluation can be undefined at singular UV
        // points (cone apex, sphere pole, degenerate seams) — skipping such
        // a vertex would otherwise leave it with the zero vector that
        // AddNormals allocated, which renders as black/garbage shading.
        if (!tri->HasNormals()) {
            tri->AddNormals();
        }
        tri->ComputeNormals();

        // Step 2: overwrite with analytic surface normals where defined.
        // Sharp creases between OCCT faces are preserved automatically — we
        // never weld vertices across face boundaries, so adjacent faces emit
        // independent glTF primitives with their own normals.
        //
        // IMPORTANT: store normals in the SURFACE frame (i.e. unflipped, no
        // reversal for TopAbs_REVERSED faces). OCCT's RWGltf_CafWriter does
        // the reversal itself when emitting the GLB — see
        // RWMesh_FaceIterator::NormalTransformed(), which calls
        // aNorm.Reverse() for REVERSED faces and pairs it with a triangle
        // winding swap in TriangleOriented(). Reversing here too gave a
        // double-flip: vertex normals ended up surface-direction while
        // triangle winding pointed topology-outward, and Blender rendered
        // half of every mirrored body dark because the loop normals fought
        // the polygon winding.
        BRepAdaptor_Surface surf(face, Standard_False);
        BRepLProp_SLProps props(surf, 1, gp::Resolution());

        const Standard_Integer n = tri->NbNodes();
        for (Standard_Integer i = 1; i <= n; ++i) {
            const gp_Pnt2d uv = tri->UVNode(i);
            props.SetParameters(uv.X(), uv.Y());
            if (!props.IsNormalDefined()) continue; // keep mesh-derived baseline
            const gp_Dir nrm = props.Normal();
            tri->SetNormal(i, gp_Vec3f((float)nrm.X(),
                                       (float)nrm.Y(),
                                       (float)nrm.Z()));
        }
        ++nFaces;
    }
    return nFaces;
}

// ── OCCT BRepMesh tessellation ──────────────────────────────────────────────
static void mesh_with_occt(const TopoDS_Shape& shape,
                           double linearDeflection,
                           double angularDeflection,
                           bool   relative,
                           double minSize) {
    IMeshTools_Parameters mp;
    mp.Deflection                = linearDeflection;
    mp.Angle                     = angularDeflection;
    mp.Relative                  = relative ? Standard_True : Standard_False;
    mp.InParallel                = Standard_True;
    mp.MinSize                   = (minSize > 0.0) ? minSize : Precision::Confusion();
    mp.InternalVerticesMode      = Standard_True;
    mp.ControlSurfaceDeflection  = Standard_True;
    mp.AdjustMinSize             = Standard_True;
    BRepMesh_IncrementalMesh mesher(shape, mp);
    (void)mesher;
}

int main(int argc, char** argv) {
    if (argc < 3) {
        print_usage();
        return 2;
    }

    std::string input  = argv[1];
    std::string output = argv[2];

    // OCCT knobs (Mayo-style absolute defaults: 1.0 mm chord, 20° angular)
    double linearDeflection  = 1.0;
    double angularDeflection = 0.349066;
    bool   relative          = false;
    double minSize           = -1.0;
    bool   useShapeFix       = true;
    bool   useAnalyticNormals = true;

    for (int i = 3; i < argc; ++i) {
        std::string a = argv[i];
        if      (a == "--linear"   && i + 1 < argc) linearDeflection  = std::atof(argv[++i]);
        else if (a == "--angular"  && i + 1 < argc) angularDeflection = std::atof(argv[++i]);
        else if (a == "--min-size" && i + 1 < argc) minSize           = std::atof(argv[++i]);
        else if (a == "--relative")                 relative          = true;
        else if (a == "--no-shape-fix")             useShapeFix       = false;
        else if (a == "--no-analytic-normals")      useAnalyticNormals = false;
        else { print_usage(); return 2; }
    }

    // ── XCAF doc + STEP read ────────────────────────────────────────────────
    Handle(XCAFApp_Application) app = XCAFApp_Application::GetApplication();
    Handle(TDocStd_Document) doc;
    app->NewDocument("BinXCAF", doc);

    STEPCAFControl_Reader reader;
    reader.SetColorMode(true);
    reader.SetNameMode(true);
    reader.SetLayerMode(true);

    IFSelect_ReturnStatus st = reader.ReadFile(input.c_str());
    if (st != IFSelect_RetDone) {
        std::cerr << "step2glb: failed to read STEP file: " << input << "\n";
        return 1;
    }
    if (!reader.Transfer(doc)) {
        std::cerr << "step2glb: failed to transfer STEP to document\n";
        return 1;
    }

    Handle(XCAFDoc_ShapeTool) shapeTool = XCAFDoc_DocumentTool::ShapeTool(doc->Main());
    Handle(XCAFDoc_ColorTool) colorTool = XCAFDoc_DocumentTool::ColorTool(doc->Main());

    // Diagnostic: dump XCAF colors.
    {
        TDF_LabelSequence colorLabels;
        colorTool->GetColors(colorLabels);
        std::cerr << "step2glb: XCAF colors in doc: " << colorLabels.Length() << "\n";
        for (Standard_Integer i = 1; i <= colorLabels.Length(); ++i) {
            Quantity_Color c;
            if (!colorTool->GetColor(colorLabels.Value(i), c)) continue;
            std::cerr << "  color[" << i << "] RGB=("
                      << int(c.Red()*255) << "," << int(c.Green()*255) << "," << int(c.Blue()*255)
                      << ")\n";
        }
    }

    // Helper to find a color for any XCAF label.
    auto findColor = [&](const TDF_Label& lbl, Quantity_Color& out) -> bool {
        if (lbl.IsNull()) return false;
        if (colorTool->GetColor(lbl, XCAFDoc_ColorSurf, out)) return true;
        if (colorTool->GetColor(lbl, XCAFDoc_ColorGen,  out)) return true;
        if (colorTool->GetColor(lbl, XCAFDoc_ColorCurv, out)) return true;
        TDF_LabelSequence subs;
        shapeTool->GetSubShapes(lbl, subs);
        for (Standard_Integer i = 1; i <= subs.Length(); ++i) {
            if (colorTool->GetColor(subs.Value(i), XCAFDoc_ColorSurf, out)) return true;
            if (colorTool->GetColor(subs.Value(i), XCAFDoc_ColorGen,  out)) return true;
            if (colorTool->GetColor(subs.Value(i), XCAFDoc_ColorCurv, out)) return true;
        }
        return false;
    };

    // ── Detect proper assembly vs monolithic compound ───────────────────────
    // A "proper assembly" has XCAF Components — the STEP file authored a real
    // hierarchy of named parts, and the user wants that hierarchy preserved
    // in Blender's outliner. We can use the source doc directly: it already
    // has the right structure, names, colours, and length unit.
    //
    // A "monolithic compound" is one big bag of solids stuffed into a single
    // top-level COMPOUND with no XCAF Component structure. The compound has
    // no semantic meaning — it's just file packaging — so we flatten it into
    // individual top-level shapes via outDoc.
    bool isAssembly = false;
    {
        // OCCT's STEP reader registers an XCAFDoc_Component for every
        // sub-shape it sees, so GetComponents() alone returns true even for
        // a monolithic bag of solids. The real signal is whether the CAD
        // author *named* the parts: a proper assembly has TDataStd_Name on
        // the majority of its components ("Felge Vorne", "M4x16"), while a
        // monolithic export leaves them anonymous.
        TDF_LabelSequence srcFree;
        shapeTool->GetFreeShapes(srcFree);
        int totalComponents = 0;
        int namedComponents = 0;
        for (Standard_Integer i = 1; i <= srcFree.Length(); ++i) {
            TDF_LabelSequence components;
            if (!shapeTool->GetComponents(srcFree.Value(i), components)) continue;
            for (Standard_Integer j = 1; j <= components.Length(); ++j) {
                ++totalComponents;
                Handle(TDataStd_Name) na;
                if (components.Value(j).FindAttribute(TDataStd_Name::GetID(), na)
                    && !na->Get().IsEmpty()) {
                    ++namedComponents;
                }
            }
        }
        // Require ≥2 components AND a majority carrying real CAD names.
        isAssembly = (totalComponents >= 2) && (namedComponents * 2 >= totalComponents);
        std::cerr << "step2glb: " << namedComponents << "/" << totalComponents
                  << " components carry XCAF names\n";
    }

    // writeDoc / writeShapeTool point at whichever doc we end up emitting.
    // Holding a Handle keeps the underlying object alive past local scope.
    Handle(TDocStd_Document)  writeDoc;
    Handle(XCAFDoc_ShapeTool) writeShapeTool;

    if (isAssembly) {
        Standard_Real srcUnit = 0.001;
        XCAFDoc_DocumentTool::GetLengthUnit(doc, srcUnit);
        std::cerr << "step2glb: proper assembly detected — preserving CAD hierarchy\n";
        std::cerr << "step2glb: length unit " << srcUnit << " m/unit\n";
        writeDoc        = doc;
        writeShapeTool  = shapeTool;
    } else {
        // ── Build flat outDoc with compound decomposition ────────────────
        // We never mutate the source doc (RemoveShape proved unreliable —
        // the GLB writer can still see compound labels even after removal).
        // Instead, build a fresh outDoc containing only the shapes we want:
        //   * Compounds with 2+ solids → add each solid individually
        //   * Everything else          → add as-is
        // OCCT TopoDS shapes share their underlying TShape by pointer, so
        // tessellation done later through outDoc reaches the same geometry.
        Handle(TDocStd_Document) outDoc;
        app->NewDocument("BinXCAF", outDoc);
        Handle(XCAFDoc_ShapeTool) outShapeTool =
            XCAFDoc_DocumentTool::ShapeTool(outDoc->Main());
        Handle(XCAFDoc_ColorTool) outColorTool =
            XCAFDoc_DocumentTool::ColorTool(outDoc->Main());

        // Copy the length unit (set by the STEP reader on doc) so the glTF
        // writer applies the correct mm → m scale. Without this, fresh
        // BinXCAF documents default to "no unit" and the model imports 1000×
        // too large.
        {
            Standard_Real srcUnit = 0.001;
            XCAFDoc_DocumentTool::GetLengthUnit(doc, srcUnit);
            XCAFDoc_DocumentTool::SetLengthUnit(outDoc, srcUnit);
            std::cerr << "step2glb: length unit " << srcUnit << " m/unit\n";
        }

        TDF_LabelSequence srcFree;
        shapeTool->GetFreeShapes(srcFree);

        for (Standard_Integer i = 1; i <= srcFree.Length(); ++i) {
            TDF_Label srcLbl = srcFree.Value(i);
            TopoDS_Shape s   = shapeTool->GetShape(srcLbl);
            if (s.IsNull()) continue;

            // Compound with multiple solids → decompose
            if (s.ShapeType() == TopAbs_COMPOUND) {
                std::vector<TopoDS_Shape> solids;
                for (TopExp_Explorer exp(s, TopAbs_SOLID); exp.More(); exp.Next())
                    solids.push_back(exp.Current());

                if (solids.size() >= 2) {
                    Quantity_Color parentColor;
                    bool parentHasColor = findColor(srcLbl, parentColor);
                    int coloredCount = 0;

                    for (const auto& sol : solids) {
                        TDF_Label origLabel;
                        bool foundOrig = shapeTool->FindSubShape(srcLbl, sol, origLabel);
                        if (!foundOrig) foundOrig = shapeTool->FindShape(sol, origLabel);

                        TCollection_ExtendedString stableName;
                        if (foundOrig) {
                            Handle(TDataStd_Name) na;
                            if (origLabel.FindAttribute(TDataStd_Name::GetID(), na))
                                stableName = na->Get();
                        }
                        if (stableName.IsEmpty()) {
                            TCollection_AsciiString entry;
                            TDF_Tool::Entry(foundOrig ? origLabel : srcLbl, entry);
                            entry.ChangeAll(':', '_');
                            stableName = TCollection_ExtendedString("Body_")
                                       + TCollection_ExtendedString(entry);
                        }

                        Quantity_Color col;
                        bool hasColor = false;
                        if (foundOrig) hasColor = findColor(origLabel, col);
                        if (!hasColor && parentHasColor) { col = parentColor; hasColor = true; }

                        TDF_Label newLbl = outShapeTool->NewShape();
                        outShapeTool->SetShape(newLbl, sol);
                        TDataStd_Name::Set(newLbl, stableName);
                        if (hasColor) {
                            outColorTool->SetColor(newLbl, col, XCAFDoc_ColorSurf);
                            ++coloredCount;
                        }
                    }

                    std::cerr << "step2glb: decomposed compound into " << solids.size()
                              << " solids (" << coloredCount << " colored)\n";
                    continue;
                }
            }

            // Pass-through: single solid, shell, or 1-solid compound
            TDF_Label newLbl = outShapeTool->NewShape();
            outShapeTool->SetShape(newLbl, s);
            {
                Handle(TDataStd_Name) na;
                if (srcLbl.FindAttribute(TDataStd_Name::GetID(), na) && !na->Get().IsEmpty())
                    TDataStd_Name::Set(newLbl, na->Get());
            }
            {
                Quantity_Color col;
                if (findColor(srcLbl, col))
                    outColorTool->SetColor(newLbl, col, XCAFDoc_ColorSurf);
            }
        }

        writeDoc       = outDoc;
        writeShapeTool = outShapeTool;
    }

    // ── Tessellate each free shape ──────────────────────────────────────────
    // BRepMesh_IncrementalMesh and TopExp_Explorer both descend recursively,
    // so this single loop handles flat outDoc (one shape per part) and
    // hierarchical doc (one shape = entire assembly compound) identically.
    TDF_LabelSequence freeLabels;
    writeShapeTool->GetFreeShapes(freeLabels);

    int shapeCount = 0;
    int healedCount = 0;
    int analyticFaceCount = 0;
    for (Standard_Integer i = 1; i <= freeLabels.Length(); ++i) {
        TDF_Label lbl   = freeLabels.Value(i);
        TopoDS_Shape shape = writeShapeTool->GetShape(lbl);
        if (shape.IsNull()) continue;

        mesh_with_occt(shape, linearDeflection, angularDeflection, relative, minSize);
        ++shapeCount;

        if (useShapeFix && count_triangles(shape) == 0) {
            TopoDS_Shape healed = shape_fix(shape);
            if (!healed.IsNull() && !healed.IsSame(shape)) {
                mesh_with_occt(healed, linearDeflection, angularDeflection, relative, minSize);
                int n = count_triangles(healed);
                if (n > 0) {
                    writeShapeTool->SetShape(lbl, healed);
                    ++healedCount;
                    std::cerr << "step2glb: ShapeFix recovered shape " << i
                              << " (" << n << " triangles)\n";
                }
            }
        }

        if (useAnalyticNormals) {
            TopoDS_Shape current = writeShapeTool->GetShape(lbl);
            if (!current.IsNull())
                analyticFaceCount += compute_analytic_normals(current);
        }
    }
    std::cerr << "step2glb: tessellated " << shapeCount
              << " top-level shape(s) (linear=" << linearDeflection
              << " angular=" << angularDeflection
              << (relative ? " relative" : " absolute");
    if (useShapeFix && healedCount > 0)
        std::cerr << ", " << healedCount << " healed";
    if (useAnalyticNormals)
        std::cerr << ", " << analyticFaceCount << " faces with analytic normals";
    std::cerr << ")\n";

    // ── Write GLB ───────────────────────────────────────────────────────────
    TColStd_IndexedDataMapOfStringString metadata;
    RWGltf_CafWriter writer(output.c_str(), Standard_True);
    writer.SetTransformationFormat(RWGltf_WriterTrsfFormat_TRS);
    writer.SetMergeFaces(Standard_True);

    Message_ProgressRange progress;
    if (!writer.Perform(writeDoc, metadata, progress)) {
        std::cerr << "step2glb: failed to write GLB: " << output << "\n";
        return 1;
    }

    std::cerr << "step2glb: wrote " << output << "\n";
    return 0;
}
