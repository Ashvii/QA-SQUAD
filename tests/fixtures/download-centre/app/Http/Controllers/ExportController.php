<?php

namespace App\Http\Controllers;

use App\Http\Requests\StoreExportRequest;
use App\Jobs\GenerateExportJob;
use App\Models\Export;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Storage;
use Symfony\Component\HttpFoundation\StreamedResponse;

class ExportController extends Controller
{
    public function index(Request $request): JsonResponse
    {
        return response()->json(
            Export::where('user_id', $request->user()->id)->latest()->paginate(20)
        );
    }

    public function store(StoreExportRequest $request): JsonResponse
    {
        $export = Export::create([
            'user_id' => $request->user()->id,
            'type' => $request->validated('type'),
            'filters' => $request->validated('filters'),
            'status' => 'pending',
        ]);

        GenerateExportJob::dispatch($export);

        return response()->json($export, 202);
    }

    public function show(Export $export): JsonResponse
    {
        $this->authorize('view', $export);

        return response()->json($export);
    }

    public function download(Request $request, Export $export): StreamedResponse
    {
        $this->authorize('view', $export);

        abort_unless($export->status === 'ready', 409, 'Export is not ready yet.');

        $file = $request->query('file', $export->path);

        return Storage::disk('exports')->download($file);
    }

    public function destroy(Export $export): JsonResponse
    {
        $this->authorize('delete', $export);

        if ($export->path) {
            Storage::disk('exports')->delete($export->path);
        }

        $export->delete();

        return response()->json(null, 204);
    }
}
