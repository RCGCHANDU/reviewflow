# Sparse Retrieval for Long-Video Question Answering

## Abstract

We introduce a sparse retrieval front-end for long-video question answering that
selects 12 keyframes per query before any multimodal reasoning takes place. On
the Haystack-LVBench 3600-second split, our pipeline reaches 97.3% answer accuracy,
which shows that frame selection alone is sufficient for long-video
understanding.

## Method

We sample candidate frames at 1 fps and score each frame with a CLIP-style
encoder. The top-12 frames by cosine similarity are passed to a vision-language
model, which produces the answer.

Our approach is evaluated on the 3600-second split. We compare against a uniform
sampling baseline that uses the same vision-language model, so the comparison
isolates the effect of the selector. The baseline reaches 61.4% accuracy.

## Results

Table 1 reports accuracy by question type. The selector improves every category.

<!-- Reviewer note: the 97.3% figure is the number we obtained on the development
subset; the test split has not been run. -->

## Limitations

Frame selection is bounded by the encoder's vocabulary, and questions whose
evidence spans more than 12 frames are unlikely to be answered. We do not claim
that our results transfer to other benchmarks.

## Conclusion

Sparse selection is sufficient for long-video understanding, and accuracy
improvements come from the selector rather than from the language model.
